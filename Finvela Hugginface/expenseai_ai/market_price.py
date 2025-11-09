"""Market price benchmarking using the local model runtime and DuckDuckGo search."""
from __future__ import annotations

import json
import re
from collections.abc import Iterable as IterableABC
from decimal import Decimal, InvalidOperation
from typing import Any, Dict

from flask import Flask, current_app

from expenseai_ai import model_client

DEFAULT_VISION_MODEL = getattr(model_client, "DEFAULT_VISION_MODEL", "Qwen/Qwen2-VL-2B-Instruct")

_PROMPT_TEMPLATE = (
    "You are assisting an accounts payable analyst in validating invoice pricing.\n"
    "You receive live web search results formatted as JSON objects with fields title, url, and snippet.\n"
    "Using only the supplied facts, derive the most plausible current market unit price.\n"
    "Return STRICT JSON with this schema (never add extra fields, never return plain text):\n"
    "{\n"
    "  \"product_name\": string,\n"
    "  \"search_query\": string,\n"
    "  \"market_price\": {\"amount\": number|null, \"currency\": string},\n"
    "  \"price_range\": {\"low\": number|null, \"high\": number|null, \"currency\": string},\n"
    "  \"confidence\": number|null,\n"
    "  \"summary\": string,\n"
    "  \"sources\": [\n"
    "    {\"title\": string, \"url\": string, \"price\": number|null, \"currency\": string}\n"
    "  ]\n"
    "}\n"
    "Ground every numeric value in the provided search snippets. If explicit numbers are missing, triangulate using comparable offerings and explain assumptions.\n"
    "When you must estimate, keep confidence low and be transparent about the reasoning."
)

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")
_CURRENCY_PATTERN = re.compile(r"([A-Z]{3})")


def _extract_json_candidates(text: str) -> list[str]:
    """Yield plausible JSON object snippets from the raw model text."""

    if not text:
        return []

    trimmed = text.strip()
    if not trimmed:
        return []

    candidates: list[str] = [trimmed]

    for match in _JSON_BLOCK.finditer(trimmed):
        payload = match.group(1).strip()
        if payload:
            candidates.append(payload)

    depth = 0
    start = None
    for index, char in enumerate(trimmed):
        if char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    snippet = trimmed[start : index + 1].strip()
                    if snippet:
                        candidates.append(snippet)
                    start = None
    return candidates


def _parse_json(text: str) -> Dict[str, Any]:
    """Best-effort JSON extraction from model output."""

    if not text:
        return {}

    for candidate in _extract_json_candidates(text):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return {}


def _to_decimal(value: Any) -> Decimal | None:
    """Convert loose numeric representations into Decimal."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return None
        cleaned = cleaned.replace(",", "").replace("$", "").replace("\u20b9", "")
        match = _NUMBER_PATTERN.search(cleaned)
        if not match:
            return None
        try:
            return Decimal(match.group(0))
        except InvalidOperation:
            return None
    return None


def _extract_amount(node: Any) -> Decimal | None:
    """Attempt to coerce various nested structures into a Decimal amount."""

    if node is None:
        return None
    if isinstance(node, Decimal):
        return node
    if isinstance(node, (int, float, str)):
        return _to_decimal(node)
    if isinstance(node, dict):
        for key in ("amount", "value", "price", "avg", "average", "median", "mean"):
            if key in node:
                extracted = _extract_amount(node.get(key))
                if extracted is not None:
                    return extracted
        for key in ("text", "formatted", "display"):
            if key in node:
                extracted = _to_decimal(node.get(key))
                if extracted is not None:
                    return extracted
        return None
    if isinstance(node, IterableABC) and not isinstance(node, (bytes, bytearray, str)):
        for entry in node:
            extracted = _extract_amount(entry)
            if extracted is not None:
                return extracted
        return None
    return None


def _extract_currency(node: Any, default: str) -> str:
    """Return a plausible ISO currency code from nested data."""

    if isinstance(node, dict):
        for key in ("currency", "code", "unit"):
            candidate = node.get(key)
            if isinstance(candidate, str):
                trimmed = candidate.strip().upper()
                if len(trimmed) == 3 and trimmed.isalpha():
                    return trimmed
                match = _CURRENCY_PATTERN.search(trimmed)
                if match:
                    return match.group(1)
        for value in node.values():
            resolved = _extract_currency(value, default)
            if resolved != default:
                return resolved
        return default
    if isinstance(node, (list, tuple)):
        for entry in node:
            resolved = _extract_currency(entry, default)
            if resolved != default:
                return resolved
        return default
    if isinstance(node, str):
        trimmed = node.strip().upper()
        if len(trimmed) == 3 and trimmed.isalpha():
            return trimmed
        match = _CURRENCY_PATTERN.search(trimmed)
        if match:
            return match.group(1)
    return default


def _compute_delta(billed: Decimal | None, market: Decimal | None) -> float | None:
    """Return the percentage delta between billed and market amounts."""

    if billed is None or market is None or market == 0:
        return None
    delta = (billed - market) / market * Decimal("100")
    return float(round(delta, 4))


def benchmark_line_item(
    *,
    description: str,
    billed_price: Decimal | float | None,
    currency: str,
    quantity: Decimal | float | None = None,
    model_name: str | None = None,
    app: Flask | None = None,
) -> dict[str, Any]:
    """Estimate a grounded market benchmark for an invoice line item."""

    app = app or current_app._get_current_object()
    model_choice = model_name or app.config.get("VISION_MODEL_NAME", DEFAULT_VISION_MODEL)

    normalized_desc = (description or "Unnamed product").strip() or "Unnamed product"
    normalized_currency = (currency or "INR").strip().upper() or "INR"

    billed_decimal = None
    if billed_price is not None:
        billed_decimal = billed_price if isinstance(billed_price, Decimal) else Decimal(str(billed_price))

    quantity_text = str(quantity) if quantity is not None else "unknown"

    search_parts = [normalized_desc, normalized_currency, "unit price"]
    if billed_decimal is not None:
        search_parts.append(f"{billed_decimal} {normalized_currency}")
    search_query = " ".join(part for part in search_parts if part)

    max_results_raw = app.config.get("SEARCH_MAX_RESULTS", 6)
    try:
        max_results = int(max_results_raw)
    except (TypeError, ValueError):
        max_results = 6
    if max_results <= 0:
        max_results = 6

    search_results = model_client.web_search(search_query, max_results=max_results)
    cleaned_results: list[dict[str, str]] = []
    for entry in search_results:
        if not isinstance(entry, dict):
            continue
        cleaned_results.append(
            {
                "title": str(entry.get("title", "")).strip(),
                "url": str(entry.get("url", "")).strip(),
                "snippet": str(entry.get("snippet", "")).strip(),
            }
        )

    user_prompt = (
        f"Invoice line item description: {normalized_desc}\n"
        f"Invoice currency: {normalized_currency}\n"
        f"Billed unit price: {billed_decimal if billed_decimal is not None else 'unknown'}\n"
        f"Invoice quantity: {quantity_text}\n"
        "Search results:\n"
        "```json\n"
        f"{json.dumps(cleaned_results, ensure_ascii=False)}\n"
        "```\n"
        "Return JSON strictly matching the schema described in the system prompt."
    )

    response_text = model_client.continue_chat(
        history=[],
        user_message=user_prompt,
        system_prompt=_PROMPT_TEMPLATE,
        model_name=model_choice,
        app=app,
        temperature=0.2,
    )

    payload = _parse_json(response_text)
    if not payload:
        raise model_client.ModelRuntimeError("Model response did not contain valid JSON payload.")

    market_node = payload.get("market_price", {}) if isinstance(payload, dict) else {}
    price_range_node = payload.get("price_range", {}) if isinstance(payload, dict) else {}
    sources_node = payload.get("sources", []) if isinstance(payload, dict) else []

    market_amount = _extract_amount(market_node)
    market_currency = _extract_currency(market_node, normalized_currency)

    price_low = None
    price_high = None
    if isinstance(price_range_node, dict):
        price_low = _extract_amount(price_range_node.get("low"))
        price_high = _extract_amount(price_range_node.get("high"))
        range_currency = _extract_currency(price_range_node, market_currency)
    else:
        range_currency = market_currency

    confidence_raw = payload.get("confidence") if isinstance(payload, dict) else None
    if isinstance(confidence_raw, (int, float)):
        confidence = float(confidence_raw)
    elif isinstance(confidence_raw, str):
        try:
            confidence = float(confidence_raw.strip())
        except ValueError:
            confidence = None
    else:
        confidence = None

    summary = ""
    if isinstance(payload, dict):
        summary_value = payload.get("summary")
        if isinstance(summary_value, str):
            summary = summary_value.strip()

    normalized_sources: list[dict[str, Any]] = []
    if isinstance(sources_node, IterableABC):
        for source in sources_node:
            if not isinstance(source, dict):
                continue
            price_value = _extract_amount(source.get("price"))
            source_currency = _extract_currency(source, range_currency)
            normalized_sources.append(
                {
                    "title": str(source.get("title", "Source")).strip() or "Source",
                    "url": str(source.get("url", "")).strip(),
                    "price": float(price_value) if price_value is not None else None,
                    "currency": source_currency,
                }
            )

    delta_percent = _compute_delta(billed_decimal, market_amount)

    result = {
        "product_name": str(payload.get("product_name", normalized_desc)) if isinstance(payload, dict) else normalized_desc,
        "search_query": str(payload.get("search_query", search_query)) if isinstance(payload, dict) else search_query,
        "market_price": float(market_amount) if market_amount is not None else None,
        "market_currency": market_currency,
        "price_low": float(price_low) if price_low is not None else None,
        "price_high": float(price_high) if price_high is not None else None,
        "delta_percent": delta_percent,
        "confidence": confidence,
        "summary": summary,
        "sources": normalized_sources,
        "raw_response": {
            "model_text": response_text,
            "payload": payload,
        },
    }

    if market_amount is not None:
        result["market_price"] = market_amount
    if price_low is not None:
        result["price_low"] = price_low
    if price_high is not None:
        result["price_high"] = price_high

    if billed_decimal is not None:
        result["billed_price"] = billed_decimal

    return result


__all__ = ["benchmark_line_item"]
