"""Gemini API adapter.

This module wraps interactions with Google Gemini 2.5 Flash Lite
using the official `google-genai` client.  When the live API call
fails or is unavailable (for example during offline development),
the adapter falls back to deterministic dummy responses so the rest
of the application can continue to operate.

Two main functions are exposed:

* `extract_Memos` – given a list of PIL images and additional
  context, return a structured representation of the Memos and
  field confidence scores.
* `generate_report` – given the structured Memos JSON, return
  a Markdown report.

These functions should be asynchronous in production but are
synchronous here for simplicity.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Tuple

from flask import current_app
from google import genai
from google.genai import types
from PIL import Image

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT_TEMPLATE = """
You are an expert Memos extraction assistant. You receive one or more Memos images and a JSON context payload.
Extract structured data and respond with a single JSON object containing the exact top-level keys below.

- Memos_number
- Memos_amount
- currency
- Memos_date
- dealer_gstin
- billed_gstin
- dealer_name
- billed_name
- items (list of objects with keys hsn, description, quantity, unit_price, gst_rate, line_total)
- taxes (list of objects with keys type, rate, amount)
- purchase_order_numbers
- payment_terms
- gst_validations (object with dealer_gstin and billed_gstin entries)
- arithmetic_check (object with valid boolean and errors list)
- duplicate_check (object with is_duplicate, duplicate_of_Memos_number, reason)
- price_outliers (list)
- confidence (number between 0 and 1)
- confidences (object mapping field name to confidence score between 0 and 1)

Use the context to influence duplicate checks and GST statuses. If information is missing, return null values.
Return ONLY raw JSON with no commentary. The context payload is:
{context_json}
"""

REPORT_PROMPT_TEMPLATE = """
You are a finance reporting assistant. Given structured Memos JSON and confidence scores, write a Markdown report.
Include a summary table with confidence values, detailed line items table, taxes table, risk summary and next steps.
Stress anomalies such as duplicates, GST mismatches, arithmetic errors and price outliers. Keep the tone professional.

Structured data:
{extracted_json}

Confidence scores:
{confidence_json}
"""

_client: genai.Client | None = None
_client_key: str | None = None


def _get_api_key() -> str:
    api_key = None
    try:
        api_key = current_app.config.get("GEMINI_API_KEY")  # type: ignore[attr-defined]
    except RuntimeError:
        # Outside of application context
        api_key = None
    if not api_key:
        api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured")
    return api_key


def _get_client() -> genai.Client:
    global _client, _client_key
    api_key = _get_api_key()
    if _client is None or api_key != _client_key:
        _client = genai.Client(api_key=api_key)
        _client_key = api_key
    return _client


def _image_to_part(image: Image.Image) -> types.Part:
    buffer = io.BytesIO()
    fmt = (image.format or "JPEG").upper()
    if fmt not in {"JPEG", "PNG", "WEBP"}:
        fmt = "JPEG"
    image.save(buffer, format=fmt)
    mime_type = f"image/{fmt.lower()}"
    return types.Part.from_bytes(data=buffer.getvalue(), mime_type=mime_type)


def _context_to_json(context: Dict[str, Any]) -> str:
    safe_context = {
        "dealer_name": context.get("dealer_name"),
        "previous_duplicates": context.get("previous_duplicates", []),
        "gst_statuses": context.get("gst_statuses", {}),
        "hsn_rates": context.get("hsn_rates", {}),
    }
    return json.dumps(safe_context, ensure_ascii=False)


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
    data.setdefault("duplicate_check", {"is_duplicate": False, "duplicate_of_Memos_number": None, "reason": ""})
    return data, {str(k): float(v) for k, v in confidences.items() if isinstance(v, (int, float))}


def _extract_Memos_via_gemini(images: List[Image.Image], context: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, float]]:
    client = _get_client()
    contents: List[Any] = [_image_to_part(image) for image in images]
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(context_json=_context_to_json(context))
    contents.append(prompt)
    response = client.models.generate_content(
        model="gemini-2.5-flash-lite",
        contents=contents,
    )
    text = getattr(response, "text", None)
    if not text and hasattr(response, "candidates"):
        pieces: List[str] = []
        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None)
            if not parts:
                continue
            for part in parts:
                if hasattr(part, "text") and part.text:
                    pieces.append(part.text)
        if pieces:
            text = "".join(pieces)
        if not text:
            raise ValueError("Gemini response did not contain text")
    if not text or not str(text).strip():
        raise ValueError("Gemini response was empty")

    serialised = str(text).strip()
    if serialised.startswith("```"):
        serialised = serialised.strip("`").strip()
        lines = serialised.splitlines()
        if lines and lines[0].lower().startswith("json"):
            lines = lines[1:]
        serialised = "\n".join(lines).strip()
    try:
        data = json.loads(serialised)
    except json.JSONDecodeError as exc:
        preview = serialised[:200].replace("\n", " ")
        raise ValueError(f"Gemini response was not valid JSON: {preview}") from exc
    return _normalise_extraction_payload(data)


def dummy_extract_from_image(images: List[Image.Image]) -> Dict[str, Any]:
    """Pretend to extract fields from Memos images.

    This function returns a canned response for demonstration.  It
    attempts to infer the Memos number and date from the file
    metadata (if the image was created from a PDF named like
    `Memos_<number>.pdf`) but otherwise falls back to defaults.
    """
    # Attempt to infer Memos number and date from image filename via regex
    Memos_number = "INV-000"
    Memos_date = datetime.utcnow().strftime("%Y-%m-%d")
    dealer_name = "Unknown Dealer"
    grand_total = 0.0
    items: List[Dict[str, Any]] = []
    # Inspect PIL image info to guess file name if possible
    try:
        path = images[0].filename  # type: ignore[attr-defined]
        match = re.search(r"(\d{3,})", os.path.basename(path))
        if match:
            Memos_number = f"INV-{match.group(1)}"
    except Exception:
        pass
    # Simple dummy item and total
    items.append({
        "hsn": "4819",
        "description": "Corrugated boxes",
        "quantity": 10,
        "unit_price": 100.0,
        "gst_rate": 18.0,
        "line_total": 1180.0,
    })
    grand_total = sum(item["line_total"] for item in items)
    return {
        "Memos_number": Memos_number,
        "Memos_amount": grand_total,
        "currency": "INR",
        "Memos_date": Memos_date,
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
        "gst_validations": {"dealer_gstin": {"status": "verified"}, "billed_gstin": {"status": "verified"}},
        "arithmetic_check": {"valid": True, "errors": []},
        "duplicate_check": {"is_duplicate": False, "duplicate_of_Memos_number": None, "reason": ""},
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
    """Extract structured Memos data using Google Gemini with graceful fallback."""
    try:
        return _extract_Memos_via_gemini(images, context)
    except ValueError as exc:
        logger.warning("Gemini extraction returned unusable payload, falling back: %s", exc)
        return _fallback_extract_Memos(images, context)
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Gemini extraction failed, falling back to dummy extractor: %s", exc)
        return _fallback_extract_Memos(images, context)


def _fallback_generate_report(extracted: Dict[str, Any], confidences: Dict[str, float]) -> str:
    """Fallback Markdown generator used when Gemini reporting is unavailable."""
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
    lines.append(f"| Duplicate | { 'Yes' if extracted['duplicate_check']['is_duplicate'] else 'No' } | - |\n")
    gst_vals = extracted.get("gst_validations", {})
    gst_summary = ", ".join(f"{k}:{v['status']}" for k,v in gst_vals.items())
    lines.append(f"| GST Validation | {gst_summary} | - |\n")
    lines.append("\n")
    # Items table
    lines.append("## Line Items\n")
    lines.append("| HSN | Description | Qty | Unit Price | GST% | Line Total |\n")
    lines.append("|----|-------------|----|-----------|------|-----------|\n")
    for item in extracted.get("items", []):
        lines.append(f"| {item['hsn']} | {item['description']} | {item['quantity']} | {item['unit_price']} | {item['gst_rate']} | {item['line_total']} |\n")
    lines.append("\n")
    # Taxes table
    lines.append("## Taxes\n")
    lines.append("| Type | Rate | Amount |\n")
    lines.append("|------|------|--------|\n")
    for tax in extracted.get("taxes", []):
        lines.append(f"| {tax['type']} | {tax['rate']} | {tax['amount']} |\n")
    lines.append("\n")
    # Risk summary
    lines.append("## Risk Summary\n")
    risk_components: List[str] = []
    # Duplicate
    risk_components.append(f"Duplicate Memos: {'Yes' if extracted['duplicate_check']['is_duplicate'] else 'No'}")
    # GST mismatches
    mismatches = [k for k, v in gst_vals.items() if v['status'] != 'verified']
    risk_components.append(f"GST mismatches: {', '.join(mismatches) if mismatches else 'None'}")
    # Arithmetic errors
    if extracted.get('arithmetic_check', {}).get('valid'):
        risk_components.append("Arithmetic errors: None")
    else:
        risk_components.append("Arithmetic errors: Present")
    # Price outliers
    outliers = extracted.get('price_outliers') or []
    risk_components.append(f"Price outliers: {len(outliers)}")
    # Risk score (simple heuristic)
    score = 0
    if extracted['duplicate_check']['is_duplicate']:
        score += 40
    if mismatches:
        score += 30
    if not extracted.get('arithmetic_check', {}).get('valid'):
        score += 20
    if outliers:
        score += 10 * len(outliers)
    lines.append("* " + "\n* ".join(risk_components) + f"\n* Overall risk score: {score}/100\n")
    # Next steps
    lines.append("\n## Next Steps\n")
    steps: List[str] = []
    if extracted['duplicate_check']['is_duplicate']:
        steps.append("Investigate duplicate Memos and cross‑verify with supplier.")
    if mismatches:
        steps.append("Verify GSTINs and request correction.")
    if not extracted.get('arithmetic_check', {}).get('valid'):
        steps.append("Recalculate totals and request an amended Memos.")
    if outliers:
        steps.append("Review pricing against market benchmarks.")
    if not steps:
        steps.append("File the Memos for payment.")
    for step in steps:
        lines.append(f"* {step}\n")
    return "".join(lines)


def generate_report(extracted: Dict[str, Any], confidences: Dict[str, float]) -> str:
    """Generate a Markdown report via Google Gemini with a graceful fallback."""
    try:
        client = _get_client()
        prompt = REPORT_PROMPT_TEMPLATE.format(
            extracted_json=json.dumps(extracted, ensure_ascii=False, indent=2),
            confidence_json=json.dumps(confidences, ensure_ascii=False, indent=2),
        )
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=[prompt],
        )
        text = getattr(response, "text", None)
        if not text and hasattr(response, "candidates"):
            pieces: List[str] = []
            for candidate in getattr(response, "candidates", []) or []:
                content = getattr(candidate, "content", None)
                parts = getattr(content, "parts", None)
                if not parts:
                    continue
                for part in parts:
                    if hasattr(part, "text") and part.text:
                        pieces.append(part.text)
            if pieces:
                text = "".join(pieces)
        if not text:
            raise ValueError("Gemini report generation returned empty text")
        return text
    except Exception as exc:  # pylint: disable=broad-except
        logger.exception("Gemini report generation failed, using fallback: %s", exc)
        return _fallback_generate_report(extracted, confidences)