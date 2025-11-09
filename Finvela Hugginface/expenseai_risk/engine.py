"""Core risk scoring computations."""
from __future__ import annotations

from dataclasses import dataclass
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional

from flask import current_app

from expenseai_benchmark import service as benchmark_service
from expenseai_ext.db import db
from expenseai_models.compliance_check import ComplianceCheck
from expenseai_models.invoice import Invoice
from expenseai_models.invoice_event import InvoiceEvent
from expenseai_models.price_benchmark import PriceBenchmark
from expenseai_models.risk_score import RiskContributor, RiskScore
from expenseai_invoices.duplicate_detection import run_manual_duplicate_checks
from expenseai_risk.weights import resolve_weights


@dataclass(slots=True)
class Contributor:
    name: str
    raw_score: float
    details: Dict[str, Any]


STATUS_FAIL = {"FAIL", "ERROR"}
STATUS_WARN = {"WARN", "NEEDS_API"}


def collect_contributors(invoice_id: int, *, benchmark_summary: dict[str, Any] | None = None) -> List[Contributor]:
    """Gather contributor inputs for the composite risk score."""
    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        raise ValueError(f"Invoice {invoice_id} not found")

    summary = benchmark_summary or benchmark_service.benchmark_invoice(invoice_id)
    max_lines = current_app.config.get("RISK_WATERFALL_MAX_CONTRIBS", 8)
    analysis_payload = _latest_analysis(invoice_id)
    analysis_data = analysis_payload[0] if analysis_payload else None
    analysis_meta = analysis_payload[1] if analysis_payload else None

    manual_duplicate: dict[str, Any] | None = None
    manual_duplicate_error: str | None = None
    try:
        manual_duplicate = run_manual_duplicate_checks(invoice)
    except Exception as exc:  # pragma: no cover - defensive logging
        current_app.logger.exception(
            "Manual duplicate detection failed during risk collection",
            extra={"invoice_id": invoice_id, "error": str(exc)},
        )
        manual_duplicate_error = str(exc)

    market_raw, market_details = _market_outlier_contributor(invoice, summary, limit=max_lines // 2 or 1)
    contributors: List[Contributor] = [
        Contributor(
            name="market_outlier",
            raw_score=market_raw,
            details=market_details,
        )
    ]

    checks = {
        check.check_type: check
        for check in ComplianceCheck.query.filter_by(invoice_id=invoice_id).all()
    }

    arithmetic_raw, arithmetic_details = _arithmetic_contributor(checks.get("ARITHMETIC"), analysis_data, analysis_meta)
    contributors.append(
        Contributor(
            name="arithmetic",
            raw_score=arithmetic_raw,
            details=arithmetic_details,
        )
    )

    hsn_raw, hsn_details = _hsn_contributor(checks.get("HSN_RATE"), analysis_data, analysis_meta)
    contributors.append(
        Contributor(
            name="hsn_rate",
            raw_score=hsn_raw,
            details=hsn_details,
        )
    )
    contributors.append(
        Contributor(
            name="gst_vendor",
            raw_score=_score_from_check(checks.get("GST_VENDOR"), warn_value=0.5),
            details=_compliance_details(checks.get("GST_VENDOR")),
        )
    )
    contributors.append(
        Contributor(
            name="gst_company",
            raw_score=_score_from_check(checks.get("GST_COMPANY"), warn_value=0.5),
            details=_compliance_details(checks.get("GST_COMPANY")),
        )
    )
    duplicate_raw, duplicate_details = _duplicate_contributor(
        analysis_data,
        analysis_meta,
        manual_duplicate,
        manual_duplicate_error,
    )
    contributors.append(
        Contributor(
            name="duplicate",
            raw_score=duplicate_raw,
            details=duplicate_details,
        )
    )
    return contributors


def compute_composite(contributors: Iterable[Contributor]) -> tuple[float, list[dict[str, Any]], str]:
    """Combine contributors into a composite score and return waterfall details with policy version."""
    weights, policy_version = resolve_weights(current_app)
    max_items = current_app.config.get("RISK_WATERFALL_MAX_CONTRIBS", 8)

    waterfall: list[dict[str, Any]] = []
    total = 0.0
    for contrib in contributors:
        weight = max(weights.get(contrib.name, 0.0), 0.0)
        raw = max(0.0, min(contrib.raw_score, 1.0))
        contribution = weight * raw
        total += contribution
        waterfall.append(
            {
                "name": contrib.name,
                "weight": weight,
                "raw_score": raw,
                "contribution": contribution,
                "details_json": contrib.details,
            }
        )
    waterfall.sort(key=lambda item: abs(item["contribution"]), reverse=True)
    if len(waterfall) > max_items:
        waterfall = waterfall[:max_items]
    composite = min(1.0, max(0.0, total))
    return composite, waterfall, policy_version


def persist_risk(
    invoice_id: int,
    composite: float,
    waterfall: list[dict[str, Any]],
    *,
    version: str = "v1",
    policy_version: str = "seed",
) -> RiskScore:
    """Persist the composite score and its contributors."""
    score = RiskScore.query.filter_by(invoice_id=invoice_id).first()
    if score is None:
        score = RiskScore(
            invoice_id=invoice_id,
            composite=composite,
            version=version,
            policy_version=policy_version,
        )
        db.session.add(score)
        db.session.flush()
    else:
        score.composite = composite
        score.version = version
        score.policy_version = policy_version
    # Remove existing contributors before inserting fresh ones.
    RiskContributor.query.filter_by(risk_score_id=score.id).delete()
    db.session.flush()

    for entry in waterfall:
        contributor = RiskContributor(
            risk_score_id=score.id,
            name=entry["name"],
            weight=float(entry.get("weight", 0.0)),
            raw_score=float(entry.get("raw_score", 0.0)),
            contribution=float(entry.get("contribution", 0.0)),
            details_json=entry.get("details_json"),
        )
        db.session.add(contributor)
    db.session.flush()
    return score


__all__ = ["collect_contributors", "compute_composite", "persist_risk", "Contributor"]


def _market_outlier_contributor(
    invoice: Invoice,
    summary: dict[str, Any],
    *,
    limit: int,
) -> tuple[float, dict[str, Any]]:
    """Derive the market outlier score from stored benchmarks or fallback summary."""

    records: List[PriceBenchmark] = list(
        sorted(
            (record for record in invoice.price_benchmarks if record.confidence is not None),
            key=lambda rec: _to_float(rec.confidence) or 0.0,
        )
    )
    if records:
        confidences = [conf for conf in (_to_float(record.confidence) for record in records) if conf is not None]
        avg_confidence = mean(confidences) if confidences else None
        raw_score = 0.0 if avg_confidence is None else max(0.0, min(1.0, 1.0 - avg_confidence))

        top_records = records[: max(limit, 1)]
        details = {
            "source": "price_benchmark",
            "avg_confidence": avg_confidence,
            "benchmarks": [
                {
                    "line_item_id": record.line_item_id,
                    "line_no": getattr(record.line_item, "line_no", None),
                    "product_name": record.product_name,
                    "billed_price": _to_float(record.billed_price),
                    "market_price": _to_float(record.market_price),
                    "delta_percent": float(record.delta_percent) if record.delta_percent is not None else None,
                    "confidence": _to_float(record.confidence),
                    "updated_at": record.updated_at.isoformat() + "Z" if record.updated_at else None,
                }
                for record in top_records
            ],
        }
        return raw_score, details

    lines = summary.get("lines", [])
    sorted_lines = sorted(lines, key=lambda item: item.get("outlier_score", 0.0), reverse=True)
    top_lines = sorted_lines[: max(limit, 1)]
    return (
        float(summary.get("avg_outlier_score", 0.0)),
        {
            "source": "historical_baseline",
            "currency": summary.get("currency"),
            "computed_at": summary.get("computed_at"),
            "top_outliers": top_lines,
        },
    )


def _duplicate_contributor(
    analysis: Optional[dict[str, Any]],
    meta: Optional[dict[str, Any]],
    manual_result: Optional[dict[str, Any]],
    manual_error: Optional[str],
) -> tuple[float, dict[str, Any]]:
    """Compute duplicate risk blending manual heuristics with analysis fallback."""

    if manual_result:
        checks = manual_result.get("checks") or []
        duplicate_checks = [check for check in checks if (check.get("status") or "").lower() == "duplicate"]
        insufficient_checks = [
            check for check in checks if (check.get("status") or "").lower() == "insufficient_data"
        ]
        raw = 1.0 if duplicate_checks else (0.2 if insufficient_checks else 0.0)
        details: dict[str, Any] = {
            "source": "manual_checks",
            "is_duplicate": bool(manual_result.get("is_duplicate")),
            "checks": checks,
            "candidate_count": manual_result.get("candidate_count"),
            "evaluated_at": manual_result.get("evaluated_at"),
        }
        if duplicate_checks:
            matched_invoices: list[dict[str, Any]] = []
            for check in duplicate_checks:
                matched_invoices.extend(check.get("matches") or [])
            details["matches"] = matched_invoices
        if insufficient_checks:
            details["insufficient_checks"] = [check.get("rule") for check in insufficient_checks]
        return raw, details

    if manual_error:
        details = {
            "source": "manual_checks",
            "status": "error",
            "error": manual_error,
        }
    else:
        details = {"source": "analysis", "status": "unknown"}

    duplicate = _analysis_section(analysis, "duplicate_check")
    if not duplicate:
        if meta:
            details["analysis_event"] = meta
        return 0.0, details

    status = (duplicate.get("status") or "").lower()
    if status == "flagged":
        raw = 1.0
    elif status == "possible":
        raw = 0.5
    else:
        raw = 0.0

    details.update(
        {
            "status": status or "unknown",
            "confidence": _to_float(duplicate.get("confidence")),
            "matches": duplicate.get("matches", []),
            "rationale": duplicate.get("rationale"),
            "source": "analysis",
        }
    )
    if meta:
        details["analysis_event"] = meta
    return raw, details


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_from_check(check: ComplianceCheck | None, *, fail_value: float = 1.0, warn_value: float = 0.5) -> float:
    if check is None:
        return 0.0
    status = (check.status or "").upper()
    if status in STATUS_FAIL:
        return fail_value
    if status in STATUS_WARN:
        return warn_value
    if status == "PASS":
        return 0.0
    return warn_value


def _compliance_details(check: ComplianceCheck | None) -> dict[str, Any]:
    if check is None:
        return {}
    details = {
        "source": "compliance",
        "status": (check.status or "").upper(),
        "score": _to_float(check.score),
    }
    if isinstance(check.summary, str):
        details["summary"] = check.summary
    if isinstance(check.details_json, dict):
        details["details"] = dict(check.details_json)
    return details


def _hsn_contributor(
    check: ComplianceCheck | None,
    analysis: Optional[dict[str, Any]],
    meta: Optional[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    section = _analysis_section(analysis, "hsn_rate_check")
    confidence = _extract_confidence(section)
    fallback = _score_from_check(check)
    raw = _score_from_confidence(confidence, fallback)
    details: dict[str, Any] = {
        "source": "analysis" if section else "compliance",
        "analysis": section or {},
        "compliance": _compliance_details(check),
        "confidence": confidence,
    }
    if meta:
        details["analysis_event"] = meta
    return raw, details


def _arithmetic_contributor(
    check: ComplianceCheck | None,
    analysis: Optional[dict[str, Any]],
    meta: Optional[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    section = _analysis_section(analysis, "arithmetic_check")
    confidence = _extract_confidence(section)
    fallback = _score_from_check(check)
    raw = _score_from_confidence(confidence, fallback)
    details: dict[str, Any] = {
        "source": "analysis" if section else "compliance",
        "analysis": section or {},
        "compliance": _compliance_details(check),
        "confidence": confidence,
    }
    if section:
        details["recomputed_totals"] = section.get("recomputed_totals")
        details["discrepancies"] = section.get("discrepancies")
    if meta:
        details["analysis_event"] = meta
    return raw, details


def _score_from_confidence(confidence: float | None, fallback: float) -> float:
    if confidence is None:
        return fallback
    clamped = max(0.0, min(confidence, 1.0))
    derived = 1.0 - clamped
    return max(0.0, min(derived, 1.0))


def _extract_confidence(section: Optional[dict[str, Any]]) -> float | None:
    if not section:
        return None
    return _to_float(section.get("confidence"))


def _analysis_section(analysis: Optional[dict[str, Any]], key: str) -> Optional[dict[str, Any]]:
    if not analysis:
        return None
    section = analysis.get(key)
    if isinstance(section, dict):
        return section
    return None


def _latest_analysis(invoice_id: int) -> Optional[tuple[dict[str, Any], dict[str, Any]]]:
    event = (
        InvoiceEvent.query.filter_by(invoice_id=invoice_id, event_type="PARSING_RESULT_SUMMARY")
        .order_by(InvoiceEvent.created_at.desc())
        .first()
    )
    if not event or not isinstance(event.payload, dict):
        return None
    analysis = event.payload.get("analysis")
    if not isinstance(analysis, dict):
        return None
    meta = {
        "event_id": event.id,
        "captured_at": event.created_at.isoformat() + "Z",
    }
    return analysis, meta
