"""Local vision-language adapter built on the Hugging Face runtime.

This module uses the shared :mod:`expenseai_ai.model_client` helper to run a
fully local, cached vision-language model (default: Qwen2-VL 2B Instruct).
It exposes two primary functions:

* ``extract_Memos`` – parse invoice images into structured JSON with
  confidence scores and duplicate hints.
* ``generate_report`` – produce a Markdown audit report from the extracted
  payload.

Both functions fall back to deterministic stubs if the local model is not
available so that the surrounding pipeline can keep running in offline or
minimal environments.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple

from flask import current_app
from PIL import Image

from expenseai_ai import model_client

logger = logging.getLogger(__name__)

DEFAULT_VISION_MODEL = getattr(model_client, "DEFAULT_VISION_MODEL", "Qwen/Qwen2-VL-2B-Instruct")

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

EXTRACTION_SYSTEM_PROMPT = (
    "You are an expert invoice extraction assistant for accounts payable. "
    "Always respond with JSON only and conform to the requested schema."
)

EXTRACTION_USER_PROMPT = (
    "Extract structured data from the attached invoice or memo images. "
    "Return a single JSON object with these top-level keys: "
    "Memos_number, Memos_amount, currency, Memos_date, dealer_gstin, billed_gstin, "
    "dealer_name, billed_name, items, taxes, purchase_order_numbers, payment_terms, "
    "gst_validations, arithmetic_check, duplicate_check, price_outliers, confidence, confidences.\n"
    "Each item in 'items' must contain hsn, description, quantity, unit_price, gst_rate, line_total.\n"
    "Each entry in 'taxes' must contain type, rate, amount.\n"
    "'gst_validations' must have dealer_gstin and billed_gstin objects.\n"
    "'arithmetic_check' must include valid (boolean) and errors (list).\n"
    "'duplicate_check' must include is_duplicate, duplicate_of_Memos_number, reason.\n"
    "For missing information, use null.\n"
    "The context payload is provided below; use it for duplicate detection and GST validation.\n"
    "Context: ```json\n{context_json}\n```\n"
    "Respond with JSON only (no code fences, no commentary)."
)

REPORT_SYSTEM_PROMPT = (
    "You are a finance reporting assistant. Use the supplied structured memo JSON "
    "and confidence scores to produce a professional Markdown report."
)

REPORT_USER_PROMPT = (
    "Draft a Markdown report covering summary metrics, confidence table, line items, taxes, "
    "risk indicators (duplicates, GST mismatches, arithmetic issues, price outliers) and "
    "recommended next actions.\n"
    "Structured data:```json\n{extracted_json}\n```\n"
    "Confidence scores:```json\n{confidence_json}\n```\n"
    "Return Markdown only."
)


def _context_to_json(context: Dict[str, Any]) -> str:
    safe_context = {
        "dealer_name": context.get("dealer_name"),
        "previous_duplicates": context.get("previous_duplicates", []),
        "gst_statuses": context.get("gst_statuses", {}),
        "hsn_rates": context.get("hsn_rates", {}),
    }
    return json.dumps(safe_context, ensure_ascii=False)


def _parse_json_candidates(text: str) -> List[str]:
    candidates: List[str] = []
    for match in _JSON_BLOCK.finditer(text):
        payload = match.group(1).strip()
        if payload:
            candidates.append(payload)
    depth = 0
    start = None
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    snippet = text[start : index + 1].strip()
                    if snippet:
                        candidates.append(snippet)
                    start = None
    return candidates


def _load_json_payload(raw_text: str) -> Dict[str, Any]:
    if not raw_text or not raw_text.strip():
        raise ValueError("Model response was empty")

    trimmed = raw_text.strip()
    if trimmed.startswith("```"):
        trimmed = trimmed.strip("`").strip()
        lines = trimmed.splitlines()
        if lines and lines[0].lower().startswith("json"):
            lines = lines[1:]
        trimmed = "\n".join(lines).strip()

    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        pass

    for candidate in _parse_json_candidates(trimmed):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValueError("Model response did not contain valid JSON")


def _normalise_extraction_payload(data: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, float]]:
    confidences = data.pop("confidences", {}) or {}
    if not isinstance(confidences, dict):
        confidences = {}
    data.setdefault("items", [])
    data.setdefault("taxes", [])
    data.setdefault("purchase_order_numbers", [])
    data.setdefault("price_outliers", [])
    data.setdefault("gst_validations", {})
    data.setdefault("arithmetic_check", {"valid": True, "errors": []})
    data.setdefault(
        "duplicate_check",
        {"is_duplicate": False, "duplicate_of_Memos_number": None, "reason": ""},
    )
    return data, {str(k): float(v) for k, v in confidences.items() if isinstance(v, (int, float))}


def dummy_extract_from_image(images: List[Image.Image]) -> Dict[str, Any]:
    """Offline fallback that returns a canned extraction payload."""

    memo_number = "INV-000"
    memo_date = datetime.utcnow().strftime("%Y-%m-%d")
    dealer_name = "Unknown Dealer"
    items: List[Dict[str, Any]] = []

    try:
        path = images[0].filename  # type: ignore[attr-defined]
        digits = re.search(r"(\d{3,})", os.path.basename(path))
        if digits:
            memo_number = f"INV-{digits.group(1)}"
    except Exception:
        pass

    items.append(
        {
            "hsn": "4819",
            "description": "Corrugated boxes",
            "quantity": 10,
            "unit_price": 100.0,
            "gst_rate": 18.0,
            "line_total": 1180.0,
        }
    )
    grand_total = sum(item["line_total"] for item in items)

    return {
        "Memos_number": memo_number,
        "Memos_amount": grand_total,
        "currency": "INR",
        "Memos_date": memo_date,
        "dealer_gstin": "27ABCDE1234F1Z5",
        "billed_gstin": "29FGHIJ5678K2L6",
        "dealer_name": dealer_name,
        "billed_name": "Client Industries",
        "items": items,
        "taxes": [
            {"type": "CGST", "rate": 9.0, "amount": 90.0},
            {"type": "SGST", "rate": 9.0, "amount": 90.0},
        ],
        "purchase_order_numbers": ["PO12345"],
        "payment_terms": "Net 30",
        "gst_validations": {
            "dealer_gstin": {"status": "verified"},
            "billed_gstin": {"status": "verified"},
        },
        "arithmetic_check": {"valid": True, "errors": []},
        "duplicate_check": {
            "is_duplicate": False,
            "duplicate_of_Memos_number": None,
            "reason": "",
        },
        "price_outliers": [],
        "confidence": 0.9,
        "confidences": {
            "Memos_number": 0.95,
            "Memos_amount": 0.9,
            "Memos_date": 0.9,
            "dealer_gstin": 0.85,
            "billed_gstin": 0.85,
        },
    }


def _fallback_extract_Memos(images: List[Image.Image], context: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, float]]:
    data = dummy_extract_from_image(images)
    prev = context.get("previous_duplicates", [])
    duplicate = False
    dup_inv = None
    for record in prev:
        if record.get("Memos_number") == data["Memos_number"]:
            duplicate = True
            dup_inv = record.get("id")
            break
    data.setdefault("duplicate_check", {})
    data["duplicate_check"]["is_duplicate"] = duplicate
    data["duplicate_check"]["duplicate_of_Memos_number"] = dup_inv

    gst_statuses = context.get("gst_statuses") or {}
    if gst_statuses:
        data["gst_validations"] = gst_statuses

    return _normalise_extraction_payload(data)


def extract_Memos(images: List[Image.Image], context: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, float]]:
    """Extract structured memo data using the local vision-language model."""

    try:
        system_prompt = EXTRACTION_SYSTEM_PROMPT
        user_prompt = EXTRACTION_USER_PROMPT.format(context_json=_context_to_json(context))
        response = model_client.generate_from_images(
            images,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            app=current_app,
            temperature=current_app.config.get("VISION_MODEL_TEMPERATURE"),
            max_new_tokens=current_app.config.get("VISION_MODEL_MAX_NEW_TOKENS"),
        )
        payload = _load_json_payload(response)
        return _normalise_extraction_payload(payload)
    except Exception as exc:  # pragma: no cover - runtime dependent
        logger.warning("Vision extraction failed, using fallback: %s", exc)
        return _fallback_extract_Memos(images, context)


def _fallback_generate_report(extracted: Dict[str, Any], confidences: Dict[str, float]) -> str:
    lines: List[str] = []
    number = extracted.get("Memos_number", "Unknown")
    date = extracted.get("Memos_date", "Unknown")
    lines.append(f"# Memos Report: {number} ({date})\n")
    lines.append("## Summary\n")
    lines.append("| Field | Value | Confidence |\n")
    lines.append("|------|------|-----------|\n")

    def add_row(field: str, value: Any) -> None:
        conf = confidences.get(field, "-")
        lines.append(f"| {field.replace('_', ' ').title()} | {value} | {conf} |\n")

    add_row("Memos_number", number)
    add_row("Memos_date", date)
    add_row("dealer_name", extracted.get("dealer_name"))
    add_row("billed_name", extracted.get("billed_name"))
    add_row("Memos_amount", f"{extracted.get('Memos_amount')} {extracted.get('currency', '')}")
    duplicate = extracted.get("duplicate_check", {}).get("is_duplicate", False)
    lines.append(f"| Duplicate | {'Yes' if duplicate else 'No'} | - |\n")
    gst_vals = extracted.get("gst_validations", {})
    gst_summary = ", ".join(f"{k}:{v['status']}" for k, v in gst_vals.items() if isinstance(v, dict))
    lines.append(f"| GST Validation | {gst_summary} | - |\n")
    lines.append("\n")
    lines.append("## Line Items\n")
    lines.append("| HSN | Description | Qty | Unit Price | GST% | Line Total |\n")
    lines.append("|----|-------------|----|-----------|------|-----------|\n")
    for item in extracted.get("items", []):
        lines.append(
            f"| {item.get('hsn')} | {item.get('description')} | {item.get('quantity')} | "
            f"{item.get('unit_price')} | {item.get('gst_rate')} | {item.get('line_total')} |\n"
        )
    lines.append("\n")
    lines.append("## Taxes\n")
    lines.append("| Type | Rate | Amount |\n")
    lines.append("|------|------|--------|\n")
    for tax in extracted.get("taxes", []):
        lines.append(f"| {tax.get('type')} | {tax.get('rate')} | {tax.get('amount')} |\n")
    lines.append("\n")
    lines.append("## Risk Summary\n")
    risk_components: List[str] = []
    risk_components.append(f"Duplicate memo: {'Yes' if duplicate else 'No'}")
    mismatches = [k for k, v in gst_vals.items() if isinstance(v, dict) and v.get('status') != 'verified']
    risk_components.append(f"GST mismatches: {', '.join(mismatches) if mismatches else 'None'}")
    arithmetic = extracted.get("arithmetic_check", {})
    if arithmetic.get("valid"):
        risk_components.append("Arithmetic errors: None")
    else:
        risk_components.append("Arithmetic errors: Present")
    outliers = extracted.get("price_outliers") or []
    risk_components.append(f"Price outliers: {len(outliers)}")
    score = 0
    if duplicate:
        score += 40
    if mismatches:
        score += 30
    if not arithmetic.get("valid"):
        score += 20
    if outliers:
        score += 10 * len(outliers)
    lines.append("* " + "\n* ".join(risk_components) + f"\n* Overall risk score: {score}/100\n")
    lines.append("\n## Next Steps\n")
    steps: List[str] = []
    if duplicate:
        steps.append("Investigate duplicate memo and verify with supplier.")
    if mismatches:
        steps.append("Validate GSTINs with the tax portal and request corrections.")
    if not arithmetic.get("valid"):
        steps.append("Recalculate totals and request an amended memo.")
    if outliers:
        steps.append("Compare pricing with market benchmarks and negotiate if needed.")
    if not steps:
        steps.append("File the memo for payment.")
    for step in steps:
        lines.append(f"* {step}\n")
    return "".join(lines)


def generate_report(extracted: Dict[str, Any], confidences: Dict[str, float]) -> str:
    """Generate a Markdown compliance report using the local model."""

    try:
        response = model_client.continue_chat(
            history=[],
            user_message=REPORT_USER_PROMPT.format(
                extracted_json=json.dumps(extracted, ensure_ascii=False, indent=2),
                confidence_json=json.dumps(confidences, ensure_ascii=False, indent=2),
            ),
            system_prompt=REPORT_SYSTEM_PROMPT,
            model_name=current_app.config.get("VISION_MODEL_NAME", DEFAULT_VISION_MODEL),
            app=current_app,
            temperature=0.1,
        )
        if not response or not response.strip():
            raise ValueError("Report generation returned empty text")
        return response
    except Exception as exc:  # pragma: no cover - runtime dependent
        logger.warning("Vision report generation failed, using fallback: %s", exc)
        return _fallback_generate_report(extracted, confidences)


__all__ = ["extract_Memos", "generate_report", "dummy_extract_from_image"]
