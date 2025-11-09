"""Manual duplicate detection utilities.

This module provides deterministic duplicate checks that do not rely on
LLM output. Multiple business rules are evaluated against historical
Memos to surface potential duplicates together with reasoning.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from ..models import Memos, Dealer

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


def _normalise_Memos_number(value: Any) -> Optional[str]:
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
    text = _normalise_text(value)
    if not text:
        return None
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        # Attempt ISO parsing with more relaxed rules
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


def _normalise_po_numbers(values: Any) -> Tuple[Set[str], Dict[str, str], List[str]]:
    normals: Set[str] = set()
    mapping: Dict[str, str] = {}
    display: List[str] = []
    if values is None:
        return normals, mapping, display
    if isinstance(values, str):
        parts: Iterable[Any] = re.split(r"[;,]", values)
    elif isinstance(values, Sequence) and not isinstance(values, (bytes, bytearray)):
        parts = values
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


def _canonical_line_items(items: Any) -> Tuple[Optional[str], int]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes, bytearray)):
        return None, 0
    normalised: List[Dict[str, Optional[str]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        normalised.append({
            "description": _normalise_text(item.get("description"), lower=True) or "",
            "quantity": _decimal_to_display(_to_decimal(item.get("quantity"))) or "",
            "unit_price": _decimal_to_display(_to_decimal(item.get("unit_price"))) or "",
            "line_total": _decimal_to_display(_to_decimal(item.get("line_total"))) or "",
            "gst_rate": _decimal_to_display(_to_decimal(item.get("gst_rate"))) or "",
            "hsn": _normalise_text(item.get("hsn"), upper=True) or "",
            "sku": _normalise_text(item.get("sku"), upper=True) or "",
        })
    if not normalised:
        return None, 0
    normalised.sort(key=lambda entry: (
        entry["description"],
        entry["quantity"],
        entry["unit_price"],
        entry["line_total"],
        entry["gst_rate"],
        entry["hsn"],
        entry["sku"],
    ))
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


def _build_snapshot(Memos: Memos) -> Dict[str, Any]:
    extracted = Memos.extracted_fields if isinstance(Memos.extracted_fields, dict) else {}
    Memos_number_raw = _first_not_none(
        extracted.get("Memos_number"),
        extracted.get("Memos_no"),
        extracted.get("number"),
    )
    dealer_gstin_raw = _first_not_none(
        Memos.dealer.gstin if Memos.dealer else None,
        extracted.get("dealer_gstin"),
        extracted.get("gstin"),
    )
    Memos_amount_raw = _first_not_none(
        extracted.get("Memos_amount"),
        extracted.get("total_amount"),
        extracted.get("grand_total"),
    )
    Memos_date_raw = _first_not_none(
        extracted.get("Memos_date"),
        extracted.get("date"),
    )
    po_raw = _first_not_none(
        extracted.get("purchase_order_numbers"),
        extracted.get("po_numbers"),
        extracted.get("po_number"),
        extracted.get("purchase_order_number"),
        extracted.get("po"),
    )
    line_signature, line_count = _canonical_line_items(extracted.get("items"))
    po_normals, po_map, po_display = _normalise_po_numbers(po_raw)
    Memos_amount = _to_decimal(Memos_amount_raw)
    return {
        "id": Memos.id,
        "Memos_number_norm": _normalise_Memos_number(Memos_number_raw),
        "Memos_number_display": Memos_number_raw,
        "dealer_gstin_norm": _normalise_gstin(dealer_gstin_raw),
        "dealer_gstin_display": dealer_gstin_raw,
        "Memos_amount": Memos_amount,
        "Memos_amount_display": _decimal_to_display(Memos_amount),
        "Memos_date_norm": _normalise_date(Memos_date_raw),
        "Memos_date_display": Memos_date_raw,
        "po_numbers_norm": po_normals,
        "po_numbers_map": po_map,
        "po_numbers_display": po_display,
        "line_signature": line_signature,
        "line_item_count": line_count,
        "checksum": Memos.checksum,
        "created_at": Memos.created_at.isoformat() if Memos.created_at else None,
        "status": Memos.status,
        "dealer_name": Memos.dealer.name if Memos.dealer else None,
        "duplicate_flag": bool(Memos.duplicate_flag),
    }


def _serialize_candidate(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "Memos_id": snapshot["id"],
        "Memos_number": snapshot.get("Memos_number_display"),
        "Memos_date": snapshot.get("Memos_date_display"),
        "Memos_amount": snapshot.get("Memos_amount_display"),
        "dealer_name": snapshot.get("dealer_name"),
        "dealer_gstin": snapshot.get("dealer_gstin_display"),
        "po_numbers": snapshot.get("po_numbers_display"),
        "line_item_count": snapshot.get("line_item_count"),
        "checksum": snapshot.get("checksum"),
        "created_at": snapshot.get("created_at"),
        "status": snapshot.get("status"),
        "duplicate_flag": snapshot.get("duplicate_flag"),
    }


def run_manual_duplicate_checks(memo: Memos) -> Dict[str, Any]:
    """Evaluate deterministic duplicate rules for the supplied memo."""
    if not isinstance(memo, Memos):
        raise TypeError("memo must be a Memos model instance")

    target = _build_snapshot(memo)

    candidate_map: Dict[int, Memos] = {}
    same_dealer = (
        Memos.query.options(joinedload(Memos.dealer))
        .filter(Memos.id != memo.id, Memos.dealer_id == memo.dealer_id)
        .all()
    )
    for inv in same_dealer:
        candidate_map[inv.id] = inv

    dealer_gstin_value = target.get("dealer_gstin_display")
    dealer_gstin_value_clean = None
    if dealer_gstin_value:
        dealer_gstin_value_clean = str(dealer_gstin_value).strip().upper()
    if dealer_gstin_value_clean:
        gstin_candidates = (
            Memos.query.options(joinedload(Memos.dealer))
            .join(Dealer, Memos.dealer_id == Dealer.id)
            .filter(Memos.id != memo.id)
            .filter(func.upper(Dealer.gstin) == dealer_gstin_value_clean)
            .all()
        )
        for inv in gstin_candidates:
            candidate_map.setdefault(inv.id, inv)

    candidate_snapshots = [_build_snapshot(inv) for inv in candidate_map.values()]
    checks: List[Dict[str, Any]] = []

    def add_check(rule: str, title: str, status: str, reason: str, matches: List[Dict[str, Any]], values: Dict[str, Any]) -> None:
        checks.append({
            "rule": rule,
            "title": title,
            "status": status,
            "reason": reason,
            "matches": matches,
            "checked_values": _to_checked_values(values),
        })

    Memos_number_norm = target.get("Memos_number_norm")
    dealer_gstin_norm = target.get("dealer_gstin_norm")
    if Memos_number_norm and dealer_gstin_norm:
        matches = []
        for cand in candidate_snapshots:
            if cand.get("Memos_number_norm") == Memos_number_norm and cand.get("dealer_gstin_norm") == dealer_gstin_norm:
                matches.append(_serialize_candidate(cand))
        if matches:
            reason = (
                "Memos number {} with dealer GSTIN {} matches Memos(s): {}.".format(
                    _display_value(target.get("Memos_number_display") or Memos_number_norm),
                    _display_value(target.get("dealer_gstin_display") or dealer_gstin_norm),
                    ", ".join(f"#{m['Memos_id']}" for m in matches),
                )
            )
            status = "duplicate"
        else:
            reason = (
                "No other Memos for GSTIN {} uses Memos number {}.".format(
                    _display_value(target.get("dealer_gstin_display") or dealer_gstin_norm),
                    _display_value(target.get("Memos_number_display") or Memos_number_norm),
                )
            )
            status = "unique"
        add_check(
            "Memos_number_dealer_gstin",
            "Memos Number + DealerGSTIN",
            status,
            reason,
            matches,
            {
                "Memos_number": target.get("Memos_number_display") or Memos_number_norm,
                "dealer_gstin": target.get("dealer_gstin_display") or dealer_gstin_norm,
            },
        )
    else:
        reason = "Missing Memos number or dealer GSTIN on this Memos; cannot evaluate uniqueness."
        add_check(
            "Memos_number_dealer_gstin",
            "Memos Number + DealerGSTIN",
            "insufficient_data",
            reason,
            [],
            {
                "Memos_number": target.get("Memos_number_display"),
                "dealer_gstin": target.get("dealer_gstin_display"),
            },
        )

    Memos_amount = target.get("Memos_amount")
    Memos_date_norm = target.get("Memos_date_norm")
    if Memos_amount is not None and Memos_date_norm and dealer_gstin_norm:
        matches = []
        for cand in candidate_snapshots:
            if cand.get("dealer_gstin_norm") != dealer_gstin_norm:
                continue
            if cand.get("Memos_amount") is None or cand.get("Memos_date_norm") is None:
                continue
            if cand.get("Memos_amount") == Memos_amount and cand.get("Memos_date_norm") == Memos_date_norm:
                matches.append(_serialize_candidate(cand))
        if matches:
            reason = (
                "Memos amount {} with date {} for GSTIN {} matches Memos(s): {}.".format(
                    _display_value(target.get("Memos_amount_display") or Memos_amount),
                    _display_value(target.get("Memos_date_display") or Memos_date_norm),
                    _display_value(target.get("dealer_gstin_display") or dealer_gstin_norm),
                    ", ".join(f"#{m['Memos_id']}" for m in matches),
                )
            )
            status = "duplicate"
        else:
            reason = (
                "No other Memos for GSTIN {} matches amount {} and date {}.".format(
                    _display_value(target.get("dealer_gstin_display") or dealer_gstin_norm),
                    _display_value(target.get("Memos_amount_display") or Memos_amount),
                    _display_value(target.get("Memos_date_display") or Memos_date_norm),
                )
            )
            status = "unique"
        add_check(
            "Memos_amount_dealer_gstin_date",
            "Memos Amount + DealerGSTIN + Date",
            status,
            reason,
            matches,
            {
                "Memos_amount": target.get("Memos_amount_display") or Memos_amount,
                "Memos_date": target.get("Memos_date_display") or Memos_date_norm,
                "dealer_gstin": target.get("dealer_gstin_display") or dealer_gstin_norm,
            },
        )
    else:
        reason = "Missing Memos amount, dealer GSTIN, or Memos date; cannot evaluate heuristic."
        add_check(
            "Memos_amount_dealer_gstin_date",
            "Memos Amount + DealerGSTIN + Date",
            "insufficient_data",
            reason,
            [],
            {
                "Memos_amount": target.get("Memos_amount_display") or Memos_amount,
                "Memos_date": target.get("Memos_date_display") or Memos_date_norm,
                "dealer_gstin": target.get("dealer_gstin_display"),
            },
        )

    po_numbers_norm: Set[str] = target.get("po_numbers_norm", set())
    if po_numbers_norm and dealer_gstin_norm:
        matches = []
        for cand in candidate_snapshots:
            cand_po_norm: Set[str] = cand.get("po_numbers_norm", set())
            if not cand_po_norm:
                continue
            overlap = sorted(po_numbers_norm & cand_po_norm)
            if not overlap:
                continue
            serial = _serialize_candidate(cand)
            serial["overlap_po_numbers"] = [
                cand.get("po_numbers_map", {}).get(value, target.get("po_numbers_map", {}).get(value, value))
                for value in overlap
            ]
            matches.append(serial)
        if matches:
            overlap_display_list = sorted({
                po
                for match in matches
                for po in (match.get("overlap_po_numbers") or match.get("po_numbers") or [])
            })
            overlap_display = ", ".join(overlap_display_list) or "listed PO"
            reason = (
                "Purchase order number overlap ({}) detected in Memos(s): {}.".format(
                    overlap_display,
                    ", ".join(f"#{m['Memos_id']}" for m in matches),
                )
            )
            status = "duplicate"
        else:
            reason = (
                "No other Memos for GSTIN {} shares purchase order numbers {}.".format(
                    _display_value(target.get("dealer_gstin_display") or dealer_gstin_norm),
                    _display_value(" / ".join(target.get("po_numbers_display", [])) or None),
                )
            )
            status = "unique"
        add_check(
            "po_number_dealer_gstin",
            "PO Number + DealerGSTIN",
            status,
            reason,
            matches,
            {
                "purchase_order_numbers": target.get("po_numbers_display"),
                "dealer_gstin": target.get("dealer_gstin_display") or dealer_gstin_norm,
            },
        )
    else:
        reason = "Missing purchase order numbers or dealer GSTIN; cannot evaluate PO overlap."
        add_check(
            "po_number_dealer_gstin",
            "PO Number + DealerGSTIN",
            "insufficient_data",
            reason,
            [],
            {
                "purchase_order_numbers": target.get("po_numbers_display"),
                "dealer_gstin": target.get("dealer_gstin_display"),
            },
        )

    checksum = target.get("checksum")
    if checksum:
        checksum_Memos = (
            Memos.query.options(joinedload(Memos.dealer))
            .filter(Memos.id != memo.id)
            .filter(Memos.checksum == checksum)
            .all()
        )
        checksum_matches = [_serialize_candidate(_build_snapshot(inv)) for inv in checksum_Memos]
        if checksum_matches:
            reason = (
                "File checksum {} already exists on Memos(s): {}.".format(
                    checksum,
                    ", ".join(f"#{m['Memos_id']}" for m in checksum_matches),
                )
            )
            status = "duplicate"
        else:
            reason = "No stored Memos shares this file checksum."
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

    line_signature = target.get("line_signature")
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
                cand.get("po_numbers_map", {}).get(value, target.get("po_numbers_map", {}).get(value, value))
                for value in overlap
            ]
            matches.append(serial)
        if matches:
            overlap_display_list = sorted({
                po
                for match in matches
                for po in (match.get("overlap_po_numbers") or match.get("po_numbers") or [])
            })
            overlap_display = ", ".join(overlap_display_list) or "listed PO"
            reason = (
                "Identical line items under PO {} also present in Memos(s): {}.".format(
                    overlap_display,
                    ", ".join(f"#{m['Memos_id']}" for m in matches),
                )
            )
            status = "duplicate"
        else:
            reason = (
                "No Memos share identical line items with purchase order numbers {}.".format(
                    _display_value(" / ".join(target.get("po_numbers_display", [])) or None),
                )
            )
            status = "unique"
        add_check(
            "line_items_po_number",
            "Line Items + PO Number",
            status,
            reason,
            matches,
            {
                "purchase_order_numbers": target.get("po_numbers_display"),
                "line_item_count": target.get("line_item_count"),
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
                "purchase_order_numbers": target.get("po_numbers_display"),
                "line_item_count": target.get("line_item_count"),
            },
        )

    is_duplicate = any(check.get("status") == "duplicate" for check in checks)
    return {
        "status": "success",
        "Memos_id": memo.id,
        "is_duplicate": is_duplicate,
        "candidate_count": len(candidate_snapshots),
        "checks": checks,
        "evaluated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
    }
