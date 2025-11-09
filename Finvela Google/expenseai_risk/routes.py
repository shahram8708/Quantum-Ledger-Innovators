"""HTTP routes exposing risk scoring operations."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify
from flask_login import current_user, login_required

from expenseai_ext.idempotency import idempotent
from expenseai_ext.security import limiter, user_or_ip_rate_limit
from expenseai_invoices.duplicate_detection import run_manual_duplicate_checks
from expenseai_models.invoice import Invoice
from expenseai_models.risk_score import RiskScore
from expenseai_risk import orchestrator
from expenseai_risk.weights import resolve_weights

risk_bp = Blueprint("expenseai_risk", __name__)


@risk_bp.route("/invoices/<int:invoice_id>/risk/run", methods=["POST"])
@login_required
@idempotent("risk")
@limiter.limit("10 per minute", key_func=user_or_ip_rate_limit())
def run_risk(invoice_id: int):
    """Trigger asynchronous risk computation for an invoice."""
    invoice = Invoice.query.get_or_404(invoice_id)
    actor = current_user.get_id() or "user"
    orchestrator.run_risk_async(invoice.id, actor=str(actor))
    return jsonify({"queued": True, "invoice_id": invoice.id})


@risk_bp.route("/invoices/<int:invoice_id>/risk/full-analysis", methods=["POST"])
@login_required
@idempotent("risk-full")
@limiter.limit("5 per minute", key_func=user_or_ip_rate_limit())
def run_full_analysis(invoice_id: int):
    """Kick off compliance, benchmarking, and risk analysis in parallel."""

    invoice = Invoice.query.get_or_404(invoice_id)
    actor = current_user.get_id() or "user"
    steps = orchestrator.run_full_analysis_async(invoice.id, actor=str(actor))
    if not steps:
        return jsonify({
            "queued": False,
            "invoice_id": invoice.id,
            "message": "No analysis steps required.",
        })

    return jsonify({"queued": True, "invoice_id": invoice.id, "steps": steps})


@risk_bp.route("/invoices/<int:invoice_id>/risk", methods=["GET"])
@login_required
def get_risk(invoice_id: int):
    """Return the latest computed risk score and contributors."""
    invoice = Invoice.query.get_or_404(invoice_id)
    score = invoice.risk_score
    weights, policy_version = resolve_weights(current_app)
    weights = {key: float(value) for key, value in weights.items()}
    manual_duplicate = None
    manual_duplicate_error: str | None = None
    try:
        manual_duplicate = run_manual_duplicate_checks(invoice)
    except Exception as exc:  # pragma: no cover - defensive logging
        manual_duplicate_error = str(exc) or "Manual duplicate check failed."
        current_app.logger.exception(
            "Manual duplicate detection failed during risk fetch",
            extra={"invoice_id": invoice.id, "error": str(exc)},
        )

    if score is None:
        payload = {
            "invoice_id": invoice.id,
            "computed": False,
            "risk_status": invoice.risk_status,
            "risk_notes": invoice.risk_notes,
            "weights": weights,
            "policy_version": policy_version,
        }
        if manual_duplicate is not None:
            payload["manual_duplicate"] = manual_duplicate
        if manual_duplicate_error:
            payload["manual_duplicate_error"] = manual_duplicate_error
        return jsonify(payload)

    payload = {
        "invoice_id": invoice.id,
        "computed": True,
        "risk_status": invoice.risk_status,
        "risk_notes": invoice.risk_notes,
        "composite": float(score.composite),
        "version": score.version,
        "policy_version": score.policy_version,
        "weights": weights,
        "contributors": [
            {
                "name": contrib.name,
                "weight": float(contrib.weight),
                "raw_score": float(contrib.raw_score),
                "contribution": float(contrib.contribution),
                "details": contrib.details_json or {},
            }
            for contrib in score.contributors
        ],
    }
    if manual_duplicate is not None:
        payload["manual_duplicate"] = manual_duplicate
    if manual_duplicate_error:
        payload["manual_duplicate_error"] = manual_duplicate_error

    if manual_duplicate is not None:
        duplicate_checks = manual_duplicate.get("checks") or []
        flagged_checks = [check for check in duplicate_checks if (check.get("status") or "").lower() == "duplicate"]
        insufficient_checks = [
            check for check in duplicate_checks if (check.get("status") or "").lower() == "insufficient_data"
        ]
        summary_parts: list[str] = []
        if flagged_checks:
            summary_parts.append(
                f"{len(flagged_checks)} manual rule{'s' if len(flagged_checks) != 1 else ''} flagged duplicates"
            )
        elif duplicate_checks:
            summary_parts.append("Manual checks reported no duplicates")
        if insufficient_checks:
            summary_parts.append(
                f"{len(insufficient_checks)} rule{'s' if len(insufficient_checks) != 1 else ''} lacked sufficient data"
            )
        if not summary_parts:
            summary_parts.append("Manual duplicate heuristics evaluated")

        manual_details = {
            "source": "manual_checks",
            "is_duplicate": bool(manual_duplicate.get("is_duplicate")),
            "checks": duplicate_checks,
            "evaluated_at": manual_duplicate.get("evaluated_at"),
            "summary": " · ".join(summary_parts),
        }
        if flagged_checks:
            matches: list[dict[str, object]] = []
            for check in flagged_checks:
                matches.extend(check.get("matches") or [])
            manual_details["matches"] = matches

        for contributor in payload["contributors"]:
            if contributor.get("name") == "duplicate":
                details = dict(contributor.get("details") or {})
                details.update(manual_details)
                contributor["details"] = details
                break
    elif manual_duplicate_error:
        for contributor in payload["contributors"]:
            if contributor.get("name") == "duplicate":
                details = dict(contributor.get("details") or {})
                details.setdefault("source", "manual_checks")
                details["manual_error"] = manual_duplicate_error
                contributor["details"] = details
                break

    for contributor in payload["contributors"]:
        if contributor.get("name") != "market_outlier":
            continue
        details = dict(contributor.get("details") or {})
        benchmarks = details.get("benchmarks")
        top_outliers = details.get("top_outliers")
        best_entry = None

        def _to_float(value):
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        if isinstance(benchmarks, list):
            for entry in benchmarks:
                if not isinstance(entry, dict):
                    continue
                delta = _to_float(entry.get("delta_percent"))
                if delta is None:
                    continue
                if best_entry is None or abs(delta) > abs(_to_float(best_entry.get("delta_percent")) or 0):
                    best_entry = entry
        elif isinstance(top_outliers, list):
            for entry in top_outliers:
                if not isinstance(entry, dict):
                    continue
                delta = _to_float(entry.get("delta_percent") or entry.get("outlier_score"))
                if delta is None:
                    continue
                if best_entry is None or abs(delta) > abs(_to_float(best_entry.get("delta_percent")) or 0):
                    best_entry = entry

        if best_entry:
            delta = _to_float(best_entry.get("delta_percent") or best_entry.get("outlier_score"))
            line_no = best_entry.get("line_no")
            descriptor = best_entry.get("description")
            billed = best_entry.get("billed_price")
            market = best_entry.get("market_price") or best_entry.get("market_average")
            summary_parts: list[str] = []
            if delta is not None:
                summary_parts.append(f"Max Δ {abs(delta):.1f}%")
            if line_no is not None:
                summary_parts.append(f"Line {line_no}")
            if descriptor:
                summary_parts.append(str(descriptor))
            summary = " · ".join(summary_parts)
            if summary:
                existing = str(details.get("summary") or "").strip()
                details["summary"] = f"{existing} · {summary}" if existing else summary
            details["top_delta_percent"] = delta
            details["top_line_no"] = line_no
            if billed is not None:
                details["top_billed_price"] = billed
            if market is not None:
                details["top_market_price"] = market
        contributor["details"] = details
        break

    return jsonify(payload)
