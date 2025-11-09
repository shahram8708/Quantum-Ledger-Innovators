"""Deterministic duplicate detection for invoices.

Ported from the legacy ``app.utils.duplicate_detector`` module but adapted
for the ExpenseAI data model. The checks here rely only on structured data
persisted in the database and avoid any LLM heuristics.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple


from expenseai_models.invoice import Invoice

DATE_FORMATS: Tuple[str, ...] = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%b %d, %Y",
    "%B %d, %Y",
)


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _normalise_text(value: Any, *, upper: bool = False, lower: bool = False) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if upper:
        return text.upper()
    if lower:
        return text.lower()
    return text


def _normalise_invoice_number(value: Any) -> Optional[str]:
    text = _normalise_text(value, upper=True)
    if not text:
        return None
    return re.sub(r"[^A-Z0-9]", "", text)


def _normalise_gstin(value: Any) -> Optional[str]:
    text = _normalise_text(value, upper=True)
    if not text:
        return None
    return re.sub(r"\s+", "", text)


def _normalise_date(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _normalise_text(value)
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return text


def _to_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        cleaned = stripped.replace(",", "")
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None
    return None


def _decimal_to_display(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _maybe_json_decode(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if not stripped:
        return value
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except (ValueError, json.JSONDecodeError):
            return value
    return value


def _normalise_po_numbers(values: Any) -> Tuple[Set[str], Dict[str, str], List[str]]:
    normals: Set[str] = set()
    mapping: Dict[str, str] = {}
    display: List[str] = []
    if values is None:
        return normals, mapping, display
    candidate = _maybe_json_decode(values)
    if isinstance(candidate, str):
        parts: Iterable[Any] = re.split(r"[;,]", candidate)
    elif isinstance(candidate, Sequence) and not isinstance(candidate, (bytes, bytearray)):
        parts = candidate
    else:
        return normals, mapping, display
    for part in parts:
        if part is None:
            continue
        text = str(part).strip()
        if not text:
            continue
        display.append(text)
        normalised = re.sub(r"[^A-Z0-9]", "", text.upper())
        if not normalised:
            continue
        normals.add(normalised)
        mapping.setdefault(normalised, text)
    return normals, mapping, display


def _canonical_line_items(invoice: Invoice) -> Tuple[Optional[str], int]:
    normalised: List[Dict[str, Optional[str]]] = []
    for item in getattr(invoice, "line_items", []) or []:
        normalised.append(
            {
                "description": _normalise_text(item.description_norm or item.description_raw, lower=True) or "",
                "quantity": _decimal_to_display(_to_decimal(item.qty)) or "",
                "unit_price": _decimal_to_display(_to_decimal(item.unit_price)) or "",
                "line_total": _decimal_to_display(_to_decimal(item.line_total or item.line_subtotal)) or "",
                "gst_rate": _decimal_to_display(_to_decimal(item.gst_rate)) or "",
                "hsn": _normalise_text(item.hsn_sac, upper=True) or "",
                "sku": "",
            }
        )
    if not normalised:
        return None, 0
    normalised.sort(
        key=lambda entry: (
            entry["description"],
            entry["quantity"],
            entry["unit_price"],
            entry["line_total"],
            entry["gst_rate"],
            entry["hsn"],
            entry["sku"],
        )
    )
    return json.dumps(normalised, sort_keys=True, ensure_ascii=False), len(normalised)


def _to_checked_values(values: Dict[str, Any]) -> Dict[str, Optional[str]]:
    checked: Dict[str, Optional[str]] = {}
    for key, value in values.items():
        if isinstance(value, Decimal):
            checked_value = _decimal_to_display(value)
        elif isinstance(value, (set, list, tuple)):
            parts = [str(part).strip() for part in value if part is not None and str(part).strip()]
            checked_value = " / ".join(parts) if parts else None
        elif value is None:
            checked_value = None
        else:
            text = str(value).strip()
            checked_value = text or None
        checked[key] = checked_value
    return checked


def _display_value(value: Any) -> str:
    if value is None:
        return "N/A"
    text = str(value).strip()
    return text or "N/A"


def _field_lookup(invoice: Invoice) -> Dict[str, Any]:
    lookup: Dict[str, Any] = {}
    for field in getattr(invoice, "extracted_fields", []) or []:
        name = (field.field_name or "").strip().lower()
        if not name:
            continue
        if name in lookup:
            continue
        lookup[name] = field.value
    return lookup


def _extract_value(lookup: Dict[str, Any], *candidates: str) -> Any:
    for key in candidates:
        value = lookup.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _build_snapshot(invoice: Invoice) -> Dict[str, Any]:
    lookup = _field_lookup(invoice)
    invoice_number_raw = _first_not_none(
        invoice.invoice_no,
        _extract_value(lookup, "invoice_number", "invoice_no", "number"),
    )
    vendor_gstin_raw = _first_not_none(
        invoice.vendor_gst,
        _extract_value(lookup, "vendor_gstin", "gstin", "supplier_gstin"),
    )
    invoice_amount_raw = _first_not_none(
        invoice.grand_total,
        invoice.subtotal,
        _extract_value(lookup, "invoice_amount", "total_amount", "grand_total"),
    )
    invoice_date_raw = _first_not_none(
        invoice.invoice_date,
        _extract_value(lookup, "invoice_date", "date"),
    )
    po_raw = _first_not_none(
        _extract_value(lookup, "purchase_order_numbers", "po_numbers"),
        _extract_value(lookup, "po_number", "purchase_order_number", "po"),
    )
    vendor_name_raw = _first_not_none(
        _extract_value(lookup, "vendor_name", "supplier_name", "seller_name"),
    )
    line_signature, line_count = _canonical_line_items(invoice)
    po_normals, po_map, po_display = _normalise_po_numbers(po_raw)
    invoice_amount = _to_decimal(invoice_amount_raw)
    return {
        "id": invoice.id,
        "invoice": invoice,
        "invoice_number_norm": _normalise_invoice_number(invoice_number_raw),
        "invoice_number_display": invoice_number_raw,
        "vendor_gstin_norm": _normalise_gstin(vendor_gstin_raw),
        "vendor_gstin_display": vendor_gstin_raw,
        "invoice_amount": invoice_amount,
        "invoice_amount_display": _decimal_to_display(invoice_amount),
        "invoice_date_norm": _normalise_date(invoice_date_raw),
        "invoice_date_display": invoice_date_raw,
        "po_numbers_norm": po_normals,
        "po_numbers_map": po_map,
        "po_numbers_display": po_display,
        "line_signature": line_signature,
        "line_item_count": line_count,
        "checksum": None,
        "created_at": invoice.created_at.isoformat() + "Z" if invoice.created_at else None,
        "status": invoice.processing_status,
        "vendor_name": vendor_name_raw,
        "duplicate_flag": False,
    }


def _serialize_candidate(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    def _serialise_value(value: Any) -> Any:
        if isinstance(value, datetime):
            return value.isoformat() + ("Z" if value.tzinfo is None else "")
        if isinstance(value, date):
            return value.isoformat()
        if isinstance(value, Decimal):
            return _decimal_to_display(value)
        return value

    return {
        "invoice_id": snapshot["id"],
        "invoice_number": _serialise_value(snapshot.get("invoice_number_display")),
        "invoice_date": _serialise_value(snapshot.get("invoice_date_display")),
        "invoice_amount": _serialise_value(snapshot.get("invoice_amount_display")),
        "vendor_name": _serialise_value(snapshot.get("vendor_name")),
        "vendor_gstin": _serialise_value(snapshot.get("vendor_gstin_display")),
        "po_numbers": snapshot.get("po_numbers_display"),
        "line_item_count": snapshot.get("line_item_count"),
        "checksum": snapshot.get("checksum"),
        "created_at": snapshot.get("created_at"),
        "status": snapshot.get("status"),
        "duplicate_flag": snapshot.get("duplicate_flag"),
    }


def _load_candidate_snapshots(target_invoice: Invoice) -> List[Dict[str, Any]]:
    query = Invoice.query.filter(Invoice.id != target_invoice.id).order_by(Invoice.created_at.desc())
    if target_invoice.organization_id is not None:
        query = query.filter(Invoice.organization_id == target_invoice.organization_id)
    else:
        query = query.filter(Invoice.organization_id.is_(None))
    # cap the search space to keep the query bounded
    candidates = query.limit(250).all()
    return [_build_snapshot(candidate) for candidate in candidates]


def run_manual_duplicate_checks(invoice: Invoice) -> Dict[str, Any]:
    """Evaluate deterministic duplicate rules for the supplied invoice."""
    if not isinstance(invoice, Invoice):
        raise TypeError("invoice must be an Invoice model instance")

    snapshot = _build_snapshot(invoice)
    candidate_snapshots = _load_candidate_snapshots(invoice)
    checks: List[Dict[str, Any]] = []

    def add_check(rule: str, title: str, status: str, reason: str, matches: List[Dict[str, Any]], values: Dict[str, Any]) -> None:
        checks.append(
            {
                "rule": rule,
                "title": title,
                "status": status,
                "reason": reason,
                "matches": matches,
                "checked_values": _to_checked_values(values),
            }
        )

    invoice_number_norm = snapshot.get("invoice_number_norm")
    vendor_gstin_norm = snapshot.get("vendor_gstin_norm")
    if invoice_number_norm and vendor_gstin_norm:
        matches = []
        for cand in candidate_snapshots:
            if cand.get("invoice_number_norm") == invoice_number_norm and cand.get("vendor_gstin_norm") == vendor_gstin_norm:
                matches.append(_serialize_candidate(cand))
        if matches:
            reason = "Invoice number {} with vendor GSTIN {} matches invoice(s): {}.".format(
                _display_value(snapshot.get("invoice_number_display") or invoice_number_norm),
                _display_value(snapshot.get("vendor_gstin_display") or vendor_gstin_norm),
                ", ".join(f"#{m['invoice_id']}" for m in matches),
            )
            status = "duplicate"
        else:
            reason = "No other invoice for GSTIN {} uses invoice number {}.".format(
                _display_value(snapshot.get("vendor_gstin_display") or vendor_gstin_norm),
                _display_value(snapshot.get("invoice_number_display") or invoice_number_norm),
            )
            status = "unique"
        add_check(
            "invoice_number_vendor_gstin",
            "Invoice Number + Vendor GSTIN",
            status,
            reason,
            matches,
            {
                "invoice_number": snapshot.get("invoice_number_display") or invoice_number_norm,
                "vendor_gstin": snapshot.get("vendor_gstin_display") or vendor_gstin_norm,
            },
        )
    else:
        reason = "Missing invoice number or vendor GSTIN on this invoice; cannot evaluate uniqueness."
        add_check(
            "invoice_number_vendor_gstin",
            "Invoice Number + Vendor GSTIN",
            "insufficient_data",
            reason,
            [],
            {
                "invoice_number": snapshot.get("invoice_number_display"),
                "vendor_gstin": snapshot.get("vendor_gstin_display"),
            },
        )

    invoice_amount = snapshot.get("invoice_amount")
    invoice_date_norm = snapshot.get("invoice_date_norm")
    if invoice_amount is not None and invoice_date_norm and vendor_gstin_norm:
        matches = []
        for cand in candidate_snapshots:
            if cand.get("vendor_gstin_norm") != vendor_gstin_norm:
                continue
            if cand.get("invoice_amount") is None or cand.get("invoice_date_norm") is None:
                continue
            if cand.get("invoice_amount") == invoice_amount and cand.get("invoice_date_norm") == invoice_date_norm:
                matches.append(_serialize_candidate(cand))
        if matches:
            reason = "Invoice amount {} with date {} for GSTIN {} matches invoice(s): {}.".format(
                _display_value(snapshot.get("invoice_amount_display") or invoice_amount),
                _display_value(snapshot.get("invoice_date_display") or invoice_date_norm),
                _display_value(snapshot.get("vendor_gstin_display") or vendor_gstin_norm),
                ", ".join(f"#{m['invoice_id']}" for m in matches),
            )
            status = "duplicate"
        else:
            reason = "No other invoice for GSTIN {} matches amount {} and date {}.".format(
                _display_value(snapshot.get("vendor_gstin_display") or vendor_gstin_norm),
                _display_value(snapshot.get("invoice_amount_display") or invoice_amount),
                _display_value(snapshot.get("invoice_date_display") or invoice_date_norm),
            )
            status = "unique"
        add_check(
            "invoice_amount_vendor_gstin_date",
            "Invoice Amount + Vendor GSTIN + Date",
            status,
            reason,
            matches,
            {
                "invoice_amount": snapshot.get("invoice_amount_display") or invoice_amount,
                "invoice_date": snapshot.get("invoice_date_display") or invoice_date_norm,
                "vendor_gstin": snapshot.get("vendor_gstin_display") or vendor_gstin_norm,
            },
        )
    else:
        reason = "Missing invoice amount, vendor GSTIN, or invoice date; cannot evaluate heuristic."
        add_check(
            "invoice_amount_vendor_gstin_date",
            "Invoice Amount + Vendor GSTIN + Date",
            "insufficient_data",
            reason,
            [],
            {
                "invoice_amount": snapshot.get("invoice_amount_display") or invoice_amount,
                "invoice_date": snapshot.get("invoice_date_display") or invoice_date_norm,
                "vendor_gstin": snapshot.get("vendor_gstin_display"),
            },
        )

    po_numbers_norm: Set[str] = snapshot.get("po_numbers_norm", set())
    if po_numbers_norm and vendor_gstin_norm:
        matches = []
        for cand in candidate_snapshots:
            cand_po_norm: Set[str] = cand.get("po_numbers_norm", set())
            if not cand_po_norm or cand.get("vendor_gstin_norm") != vendor_gstin_norm:
                continue
            overlap = sorted(po_numbers_norm & cand_po_norm)
            if not overlap:
                continue
            serial = _serialize_candidate(cand)
            serial["overlap_po_numbers"] = [
                cand.get("po_numbers_map", {}).get(value, snapshot.get("po_numbers_map", {}).get(value, value))
                for value in overlap
            ]
            matches.append(serial)
        if matches:
            overlap_display_list = sorted(
                {
                    po
                    for match in matches
                    for po in (match.get("overlap_po_numbers") or match.get("po_numbers") or [])
                }
            )
            overlap_display = ", ".join(overlap_display_list) or "listed PO"
            reason = "Purchase order number overlap ({}) detected in invoice(s): {}.".format(
                overlap_display,
                ", ".join(f"#{m['invoice_id']}" for m in matches),
            )
            status = "duplicate"
        else:
            reason = "No other invoice for GSTIN {} shares purchase order numbers {}.".format(
                _display_value(snapshot.get("vendor_gstin_display") or vendor_gstin_norm),
                _display_value(" / ".join(snapshot.get("po_numbers_display", [])) or None),
            )
            status = "unique"
        add_check(
            "po_number_vendor_gstin",
            "PO Number + Vendor GSTIN",
            status,
            reason,
            matches,
            {
                "purchase_order_numbers": snapshot.get("po_numbers_display"),
                "vendor_gstin": snapshot.get("vendor_gstin_display") or vendor_gstin_norm,
            },
        )
    else:
        reason = "Missing purchase order numbers or vendor GSTIN; cannot evaluate PO overlap."
        add_check(
            "po_number_vendor_gstin",
            "PO Number + Vendor GSTIN",
            "insufficient_data",
            reason,
            [],
            {
                "purchase_order_numbers": snapshot.get("po_numbers_display"),
                "vendor_gstin": snapshot.get("vendor_gstin_display"),
            },
        )

    checksum = snapshot.get("checksum")
    if checksum:
        checksum_matches = []
        for cand in candidate_snapshots:
            if cand.get("checksum") == checksum:
                checksum_matches.append(_serialize_candidate(cand))
        if checksum_matches:
            reason = "File checksum {} already exists on invoice(s): {}.".format(
                checksum,
                ", ".join(f"#{m['invoice_id']}" for m in checksum_matches),
            )
            status = "duplicate"
        else:
            reason = "No stored invoice shares this file checksum."
            status = "unique"
        add_check(
            "file_hash",
            "File Hash",
            status,
            reason,
            checksum_matches,
            {
                "checksum": checksum,
            },
        )
    else:
        reason = "Checksum not recorded; exact file duplicate check unavailable."
        add_check(
            "file_hash",
            "File Hash",
            "insufficient_data",
            reason,
            [],
            {
                "checksum": None,
            },
        )

    line_signature = snapshot.get("line_signature")
    if line_signature and po_numbers_norm:
        matches = []
        for cand in candidate_snapshots:
            if cand.get("line_signature") != line_signature:
                continue
            overlap = sorted(po_numbers_norm & cand.get("po_numbers_norm", set()))
            if not overlap:
                continue
            serial = _serialize_candidate(cand)
            serial["overlap_po_numbers"] = [
                cand.get("po_numbers_map", {}).get(value, snapshot.get("po_numbers_map", {}).get(value, value))
                for value in overlap
            ]
            matches.append(serial)
        if matches:
            overlap_display_list = sorted(
                {
                    po
                    for match in matches
                    for po in (match.get("overlap_po_numbers") or match.get("po_numbers") or [])
                }
            )
            overlap_display = ", ".join(overlap_display_list) or "listed PO"
            reason = "Identical line items under PO {} also present in invoice(s): {}.".format(
                overlap_display,
                ", ".join(f"#{m['invoice_id']}" for m in matches),
            )
            status = "duplicate"
        else:
            reason = "No invoices share identical line items with purchase order numbers {}.".format(
                _display_value(" / ".join(snapshot.get("po_numbers_display", [])) or None),
            )
            status = "unique"
        add_check(
            "line_items_po_number",
            "Line Items + PO Number",
            status,
            reason,
            matches,
            {
                "purchase_order_numbers": snapshot.get("po_numbers_display"),
                "line_item_count": snapshot.get("line_item_count"),
            },
        )
    else:
        reason = "Missing line items or purchase order numbers; cannot evaluate line-item overlap."
        add_check(
            "line_items_po_number",
            "Line Items + PO Number",
            "insufficient_data",
            reason,
            [],
            {
                "purchase_order_numbers": snapshot.get("po_numbers_display"),
                "line_item_count": snapshot.get("line_item_count"),
            },
        )

    is_duplicate = any(check.get("status") == "duplicate" for check in checks)
    return {
        "status": "success",
        "invoice_id": invoice.id,
        "is_duplicate": is_duplicate,
        "candidate_count": len(candidate_snapshots),
        "checks": checks,
        "evaluated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
