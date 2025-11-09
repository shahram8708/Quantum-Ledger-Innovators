"""Background orchestration for risk scoring pipeline."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from flask import current_app

from expenseai_ai import market_price as market_price_service
from expenseai_benchmark import service as benchmark_service
from expenseai_compliance import orchestrator as compliance_orchestrator
from expenseai_ext.db import db
from expenseai_models import AuditLog
from expenseai_models.invoice import Invoice
from expenseai_models.invoice_event import InvoiceEvent
from expenseai_models.price_benchmark import PriceBenchmark
from expenseai_risk.engine import collect_contributors, compute_composite, persist_risk

RISK_VERSION = "v1"
AUTO_ANALYSIS_COOLDOWN_SECONDS = 180


def run_risk_async(invoice_id: int, actor: str = "system") -> None:
    """Spawn a background worker to compute risk scores."""
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_run_with_context,
        args=(app, invoice_id, actor),
        name="risk-runner",
        daemon=True,
    )
    thread.start()


def _run_with_context(app, invoice_id: int, actor: str) -> None:
    with app.app_context():
        run_risk_pipeline(invoice_id, actor=actor)


def run_risk_pipeline(invoice_id: int, actor: str = "system") -> None:
    """Execute the risk scoring workflow synchronously."""
    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        current_app.logger.warning("Risk pipeline invoked for missing invoice", extra={"invoice_id": invoice_id})
        return

    try:
        invoice.set_risk_status("IN_PROGRESS", notes=None)
        InvoiceEvent.record(
            invoice,
            "RISK_STARTED",
            {"invoice_id": invoice.id, "actor": actor, "timestamp": datetime.utcnow().isoformat() + "Z"},
        )
        db.session.commit()
        AuditLog.log(action="risk_run_started", entity="invoice", entity_id=invoice.id, data={"actor": actor})

        benchmark_service.ingest_invoice_line_items(invoice.id)
        db.session.commit()

        summary = benchmark_service.benchmark_invoice(invoice.id)
        contributors = collect_contributors(invoice.id, benchmark_summary=summary)
        composite, waterfall, policy_version = compute_composite(contributors)
        score = persist_risk(
            invoice.id,
            composite,
            waterfall,
            version=RISK_VERSION,
            policy_version=policy_version,
        )

        invoice.set_risk_status("READY")
        invoice.risk_notes = f"Composite risk score {composite:.2f}"

        top_contribs = [
            {
                "name": entry["name"],
                "weight": entry["weight"],
                "raw_score": entry["raw_score"],
                "contribution": entry["contribution"],
            }
            for entry in waterfall
        ]
        payload = {
            "invoice_id": invoice.id,
            "composite": composite,
            "avg_outlier_score": summary.get("avg_outlier_score"),
            "contributors": top_contribs,
        }
        InvoiceEvent.record(invoice, "RISK_SUMMARY", payload)
        InvoiceEvent.record(
            invoice,
            "RISK_READY",
            {
                "invoice_id": invoice.id,
                "composite": composite,
                "version": score.version,
                "policy_version": score.policy_version,
            },
        )
        db.session.commit()
        AuditLog.log(
            action="risk_run_completed",
            entity="invoice",
            entity_id=invoice.id,
            data={"composite": composite, "contributors": top_contribs},
        )
    except Exception as exc:  # pragma: no cover - defensive path
        current_app.logger.exception("Risk pipeline failed", extra={"invoice_id": invoice_id})
        db.session.rollback()
        invoice = db.session.get(Invoice, invoice_id)
        if invoice:
            invoice.set_risk_status("ERROR", notes=str(exc), emit_event=False)
            InvoiceEvent.record(
                invoice,
                "RISK_ERROR",
                {"invoice_id": invoice.id, "error": str(exc)},
            )
            db.session.commit()
    else:
        current_app.logger.info(
            "Risk pipeline completed",
            extra={"invoice_id": invoice_id, "status": invoice.risk_status},
        )


def _plan_full_analysis(invoice: Invoice, *, force: bool) -> list[str]:
    """Return analysis steps that should run for the invoice."""
    processing_status = (invoice.processing_status or "").upper()
    if not force and processing_status != "READY":
        return []

    steps: list[str] = []
    risk_status = (invoice.risk_status or "").upper()
    if force or risk_status in {"PENDING", "ERROR"}:
        steps.append("risk")

    compliance_status = (invoice.compliance_status or "").upper()
    if force or compliance_status in {"PENDING", "ERROR"}:
        steps.append("compliance")

    if force or not invoice.price_benchmarks:
        steps.append("price_benchmarks")

    return steps


def _has_recent_auto_trigger(invoice_id: int, cooldown_seconds: int) -> bool:
    """Return True if auto analysis was triggered recently for the invoice."""

    cutoff = datetime.utcnow() - timedelta(seconds=cooldown_seconds)
    latest = (
        InvoiceEvent.query.filter_by(invoice_id=invoice_id, event_type="AUTO_ANALYSIS_TRIGGERED")
        .order_by(InvoiceEvent.created_at.desc())
        .first()
    )
    if latest is None:
        return False
    return latest.created_at >= cutoff


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, (int, Decimal)):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _update_price_benchmarks(invoice_id: int) -> dict[str, Any]:
    """Compute market price benchmarks for line items and persist results."""

    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        return {"status": "error", "message": f"Invoice {invoice_id} not found."}

    line_items = sorted(
        (item for item in invoice.line_items if item.unit_price is not None),
        key=lambda li: li.line_no,
    )
    if not line_items:
        return {
            "status": "skipped",
            "message": "No line items with a unit price are available for benchmarking.",
        }

    max_items = current_app.config.get("MARKET_PRICE_MAX_ITEMS", 5)
    try:
        max_items_int = int(max_items)
    except (TypeError, ValueError):
        max_items_int = 5
    if max_items_int > 0:
        line_items = line_items[:max_items_int]

    currency = (invoice.currency or "INR").upper()
    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for item in line_items:
        description = item.description_norm or item.description_raw or ""
        try:
            benchmark = market_price_service.benchmark_line_item(
                description=description,
                billed_price=item.unit_price,
                currency=currency,
                quantity=item.qty,
                app=current_app,
            )
        except Exception as exc:  # pragma: no cover - external API resilience
            current_app.logger.exception(
                "Auto price benchmark failed",
                extra={"invoice_id": invoice.id, "line_item_id": item.id, "error": str(exc)},
            )
            errors.append({"line_item_id": item.id, "message": str(exc)})
            continue

        record = (
            PriceBenchmark.query.filter_by(invoice_id=invoice.id, line_item_id=item.id)
            .one_or_none()
        )
        if record is None:
            record = PriceBenchmark(invoice_id=invoice.id, line_item_id=item.id)
            db.session.add(record)

        record.product_name = benchmark.get("product_name")
        record.search_query = benchmark.get("search_query")
        record.billed_price = item.unit_price
        record.billed_currency = currency
        record.market_price = _coerce_decimal(benchmark.get("market_price"))
        record.market_currency = (benchmark.get("market_currency") or currency).upper()
        record.price_low = _coerce_decimal(benchmark.get("price_low"))
        record.price_high = _coerce_decimal(benchmark.get("price_high"))
        record.delta_percent = _coerce_float(benchmark.get("delta_percent"))
        record.summary = benchmark.get("summary")
        record.confidence = _coerce_float(benchmark.get("confidence"))

        normalized_sources: list[dict[str, Any]] = []
        for source in benchmark.get("sources", []):
            if not isinstance(source, dict):
                continue
            normalized_sources.append(
                {
                    "title": str(source.get("title", "Source")),
                    "url": str(source.get("url", "")),
                    "price": _coerce_float(source.get("price")),
                    "currency": str(source.get("currency") or record.market_currency or currency).upper(),
                }
            )
        record.sources_json = normalized_sources
        record.raw_response = benchmark.get("raw_response")
        results.append(record.as_dict())

    if not results and errors:
        db.session.rollback()
        return {
            "status": "error",
            "errors": errors,
            "message": "Market price lookup failed for all eligible line items.",
        }

    run_timestamp = None
    if results:
        db.session.flush()
        run_timestamp = datetime.utcnow().isoformat() + "Z"
        InvoiceEvent.record(
            invoice,
            "PRICE_BENCHMARK",
            {
                "line_items": [
                    {
                        "line_item_id": entry["line_item_id"],
                        "line_no": entry.get("line_no"),
                        "delta_percent": entry.get("delta_percent"),
                        "market_price": entry.get("market_price"),
                        "market_currency": entry.get("market_currency"),
                    }
                    for entry in results
                ],
                "run_at": run_timestamp,
                "errors": errors,
            },
        )
    db.session.commit()

    status = "ok"
    message = None
    if errors:
        status = "partial"
        message = "Market price lookups completed with warnings."

    return {
        "status": status,
        "processed": len(results),
        "errors": errors,
        "run_at": run_timestamp,
        "message": message,
    }


def _step_kwargs(step: str, invoice_id: int, actor: str) -> dict[str, Any]:
    if step in {"risk", "compliance"}:
        return {"invoice_id": invoice_id, "actor": actor}
    return {"invoice_id": invoice_id}


def _run_full_analysis_with_context(app, invoice_id: int, actor: str, steps: list[str], force: bool) -> None:
    with app.app_context():
        _run_full_analysis(invoice_id, actor=actor, steps=steps, force=force)


def _run_full_analysis(invoice_id: int, *, actor: str, steps: list[str], force: bool) -> None:
    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        current_app.logger.warning(
            "Auto analysis skipped for missing invoice",
            extra={"invoice_id": invoice_id},
        )
        return

    dynamic_steps = _plan_full_analysis(invoice, force=force)
    planned = steps or dynamic_steps
    if not force:
        planned = [step for step in planned if step in dynamic_steps]

    if not planned:
        current_app.logger.info(
            "Auto analysis skipped because no steps are pending",
            extra={"invoice_id": invoice_id},
        )
        InvoiceEvent.record(
            invoice,
            "AUTO_ANALYSIS_SKIPPED",
            {
                "invoice_id": invoice.id,
                "actor": actor,
                "reason": "no pending steps",
                "force": force,
            },
        )
        db.session.commit()
        return

    InvoiceEvent.record(
        invoice,
        "AUTO_ANALYSIS_STARTED",
        {"invoice_id": invoice.id, "actor": actor, "steps": planned, "force": force},
    )
    db.session.commit()

    app_obj = current_app._get_current_object()
    results: dict[str, Any] = {}
    errors: list[dict[str, Any]] = []

    step_functions: dict[str, Callable[..., Any]] = {
        "risk": run_risk_pipeline,
        "compliance": compliance_orchestrator.run_compliance,
        "price_benchmarks": _update_price_benchmarks,
    }

    def _run_step(step_name: str, func: Callable[..., Any], kwargs: dict[str, Any]) -> None:
        with app_obj.app_context():
            try:
                outcome = func(**kwargs)
            except Exception as exc:  # pragma: no cover - defensive logging
                db.session.rollback()
                current_app.logger.exception(
                    "Auto analysis step failed",
                    extra={"invoice_id": invoice_id, "step": step_name},
                )
                errors.append({"step": step_name, "error": str(exc)})
            else:
                if isinstance(outcome, dict):
                    results[step_name] = outcome
                    if outcome.get("status") == "error":
                        error_entry: dict[str, Any] = {
                            "step": step_name,
                            "error": outcome.get("message") or "Step returned error status.",
                        }
                        if outcome.get("errors"):
                            error_entry["details"] = outcome["errors"]
                        errors.append(error_entry)
                elif outcome is not None:
                    results[step_name] = {"status": "ok", "data": outcome}
                else:
                    results[step_name] = {"status": "ok"}
            finally:
                db.session.remove()

    threads: list[threading.Thread] = []
    parallel_steps = [step for step in planned if step != "risk"]
    risk_requested = "risk" in planned

    for step_name in parallel_steps:
        func = step_functions.get(step_name)
        if func is None:
            message = f"Unknown analysis step '{step_name}'"
            current_app.logger.warning(
                message,
                extra={"invoice_id": invoice_id},
            )
            results[step_name] = {"status": "skipped", "message": message}
            errors.append({"step": step_name, "error": message})
            continue

        thread = threading.Thread(
            target=_run_step,
            args=(step_name, func, _step_kwargs(step_name, invoice_id, actor)),
            name=f"invoice-{invoice_id}-{step_name}",
            daemon=True,
        )
        thread.start()
        threads.append(thread)

    for thread in threads:
        thread.join()

    if risk_requested:
        func = step_functions.get("risk")
        if func is None:
            message = "Unknown analysis step 'risk'"
            current_app.logger.warning(message, extra={"invoice_id": invoice_id})
            results["risk"] = {"status": "skipped", "message": message}
            errors.append({"step": "risk", "error": message})
        else:
            _run_step("risk", func, _step_kwargs("risk", invoice_id, actor))

    refreshed = db.session.get(Invoice, invoice_id) or invoice
    event_type = "AUTO_ANALYSIS_COMPLETED" if not errors else "AUTO_ANALYSIS_PARTIAL"
    InvoiceEvent.record(
        refreshed,
        event_type,
        {
            "invoice_id": invoice_id,
            "actor": actor,
            "steps": planned,
            "results": results,
            "errors": errors,
            "force": force,
            "risk_status": getattr(refreshed, "risk_status", None),
            "compliance_status": getattr(refreshed, "compliance_status", None),
        },
    )
    db.session.commit()

    log_payload = {"invoice_id": invoice_id, "steps": planned}
    if errors:
        log_payload["errors"] = errors
        current_app.logger.warning("Auto analysis finished with warnings", extra=log_payload)
    else:
        current_app.logger.info("Auto analysis finished", extra=log_payload)


def run_full_analysis_async(invoice_id: int, actor: str = "system", *, force: bool = False) -> list[str]:
    """Kick off compliance, benchmarking, and risk analysis concurrently."""

    invoice = db.session.get(Invoice, invoice_id)
    if invoice is None:
        current_app.logger.warning(
            "Auto analysis requested for missing invoice",
            extra={"invoice_id": invoice_id},
        )
        return []

    steps = _plan_full_analysis(invoice, force=force)
    if not steps:
        current_app.logger.info(
            "Auto analysis skipped because invoice is not ready or already processed",
            extra={"invoice_id": invoice_id},
        )
        return []

    if not force and _has_recent_auto_trigger(invoice.id, AUTO_ANALYSIS_COOLDOWN_SECONDS):
        current_app.logger.info(
            "Auto analysis already triggered recently; skipping duplicate request",
            extra={"invoice_id": invoice_id},
        )
        return []

    actor_label = actor or "system"
    InvoiceEvent.record(
        invoice,
        "AUTO_ANALYSIS_TRIGGERED",
        {"invoice_id": invoice.id, "actor": actor_label, "steps": steps, "force": force},
    )
    db.session.commit()

    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_run_full_analysis_with_context,
        args=(app, invoice_id, actor_label, steps, force),
        name=f"invoice-{invoice_id}-analysis",
        daemon=True,
    )
    thread.start()
    return steps