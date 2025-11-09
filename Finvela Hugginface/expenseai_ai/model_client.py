"""Local multimodal model runtime built around open-source vision-language models.

The runtime downloads models once via Hugging Face, supports OCR through
vision-language reasoning (defaulting to Qwen2-VL 2B Instruct), and provides
helpers for invoice parsing, chat flows, embeddings, and real-time web search.
"""
from __future__ import annotations

import io
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import torch
from flask import Flask, current_app
from PIL import Image

try:  # pragma: no cover - optional dependency resolved at runtime
    import fitz  # type: ignore[import-untyped]
except Exception:  # pragma: no cover - allow informative failure later
    fitz = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency resolved at runtime
    from duckduckgo_search import DDGS
except Exception:  # pragma: no cover - allow informative failure later
    DDGS = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency resolved at runtime
    from sentence_transformers import SentenceTransformer
except Exception:  # pragma: no cover - allow informative failure later
    SentenceTransformer = None  # type: ignore[assignment]

try:  # pragma: no cover - optional dependency resolved at runtime
    from transformers import AutoModelForCausalLM, AutoProcessor
except Exception:  # pragma: no cover - allow informative failure later
    AutoModelForCausalLM = None  # type: ignore[assignment]
    AutoProcessor = None  # type: ignore[assignment]

DEFAULT_VISION_MODEL = "Qwen/Qwen2-VL-2B-Instruct"
DEFAULT_EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_IMAGE_EDGE = 1920

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class ModelRuntimeError(RuntimeError):
    """Raised when the local model runtime cannot satisfy a request."""


@dataclass
class VisionLanguageBundle:
    """Container for a loaded vision-language model and its processor."""

    model: Any
    processor: Any
    tokenizer: Any
    device: str


@dataclass
class EmbeddingBundle:
    """Container for a loaded sentence-transformer embedding model."""

    model: Any
    device: str


_VL_LOCK = threading.Lock()
_VL_BUNDLES: Dict[str, VisionLanguageBundle] = {}
_EMBED_LOCK = threading.Lock()
_EMBED_BUNDLES: Dict[str, EmbeddingBundle] = {}


def _resolve_app(app: Flask | None = None) -> Flask:
    return app or current_app._get_current_object()


def _resolve_device(preference: str | None) -> str:
    pref = (preference or "auto").strip().lower()
    if pref in {"cuda", "gpu"} and torch.cuda.is_available():
        return "cuda"
    if pref in {"mps"} and torch.backends.mps.is_available():  # pragma: no cover - macOS specific
        return "mps"
    if pref in {"cpu"}:
        return "cpu"
    if pref == "auto":
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():  # pragma: no cover - macOS specific
            return "mps"
    return "cpu"


def _ensure_transformers_available() -> None:
    if AutoModelForCausalLM is None or AutoProcessor is None:
        raise ModelRuntimeError(
            "transformers is not installed. Please install the project dependencies before using the local model runtime."
        )


def _ensure_sentence_transformers_available() -> None:
    if SentenceTransformer is None:
        raise ModelRuntimeError(
            "sentence-transformers is not installed. Please install the project dependencies before requesting embeddings."
        )


def _ensure_search_available() -> None:
    if DDGS is None:
        raise ModelRuntimeError(
            "duckduckgo-search is not installed. Please install project dependencies to use real-time search."
        )


def _vl_bundle_key(model_name: str, device: str) -> str:
    return f"{model_name}::{device}"


def _load_vl_bundle(app: Flask, *, model_name: str | None = None, ensure_loaded: bool = True) -> VisionLanguageBundle:
    _ensure_transformers_available()
    cfg = app.config
    name = model_name or cfg.get("VISION_MODEL_NAME", DEFAULT_VISION_MODEL)
    device_pref = cfg.get("VISION_MODEL_DEVICE", "auto")
    device = _resolve_device(device_pref)
    key = _vl_bundle_key(name, device)

    with _VL_LOCK:
        if key in _VL_BUNDLES:
            return _VL_BUNDLES[key]
        if not ensure_loaded:
            # Caller only wanted validation; dependencies already confirmed above.
            raise ModelRuntimeError(
                "Vision-language model has not been loaded yet. Trigger a request that requires it to download the weights."
            )
        torch_dtype = torch.float16 if device == "cuda" else torch.float32
        try:
            processor = AutoProcessor.from_pretrained(name, trust_remote_code=True)
            model = AutoModelForCausalLM.from_pretrained(
                name,
                torch_dtype=torch_dtype,
                device_map="auto" if device != "cpu" else None,
                trust_remote_code=True,
            )
            if device == "cpu":
                model = model.to(device)
            model.eval()
        except Exception as exc:  # pragma: no cover - depends on environment
            raise ModelRuntimeError(f"Failed to load vision-language model '{name}': {exc}") from exc

        tokenizer = getattr(processor, "tokenizer", None)
        if tokenizer is None:
            tokenizer = getattr(processor, "tokenizer_class", None)
        bundle = VisionLanguageBundle(model=model, processor=processor, tokenizer=tokenizer, device=device)
        _VL_BUNDLES[key] = bundle
        return bundle


def _load_embed_bundle(app: Flask, *, model_name: str | None = None) -> EmbeddingBundle:
    _ensure_sentence_transformers_available()
    cfg = app.config
    name = model_name or cfg.get("EMBEDDING_MODEL_NAME", DEFAULT_EMBED_MODEL)
    device_pref = cfg.get("EMBEDDING_DEVICE", "auto")
    device = _resolve_device(device_pref)
    key = f"{name}::{device}"

    with _EMBED_LOCK:
        if key in _EMBED_BUNDLES:
            return _EMBED_BUNDLES[key]
        try:
            model = SentenceTransformer(name, device=device)
        except Exception as exc:  # pragma: no cover - depends on environment
            raise ModelRuntimeError(f"Failed to load embedding model '{name}': {exc}") from exc
        bundle = EmbeddingBundle(model=model, device=device)
        _EMBED_BUNDLES[key] = bundle
        return bundle


def _thumbnail(image: Image.Image) -> Image.Image:
    copied = image.copy()
    copied = copied.convert("RGB")
    copied.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    return copied


def _load_document_images(path: Path, *, max_pages: int, app: Flask) -> list[Image.Image]:
    if not path.exists():
        raise FileNotFoundError(f"Invoice source file not found: {path}")
    suffix = path.suffix.lower()
    images: list[Image.Image] = []
    if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff"}:
        images.append(_thumbnail(Image.open(path)))
        return images
    if suffix == ".pdf":
        if fitz is None:
            raise ModelRuntimeError("PyMuPDF is required for PDF vision parsing. Install 'pymupdf' and retry.")
        try:
            document = fitz.open(path)
        except Exception as exc:  # pragma: no cover - depends on file
            raise ModelRuntimeError(f"Unable to open PDF '{path}': {exc}") from exc
        try:
            upper = min(max_pages, len(document))
            for index in range(upper):
                page = document[index]
                pix = page.get_pixmap(dpi=220)
                buffer = io.BytesIO(pix.tobytes("png"))
                image = Image.open(buffer)
                image.filename = f"{path.name}-page{index + 1}"
                images.append(_thumbnail(image))
        finally:
            document.close()
        if images:
            return images
        raise ModelRuntimeError(f"No renderable pages found in PDF '{path}'")
    raise ModelRuntimeError(f"Unsupported document type '{suffix}' for local vision parsing")


def generate_from_images(
    images: Sequence[Image.Image],
    *,
    system_prompt: str,
    user_prompt: str,
    model_name: str | None = None,
    app: Flask | None = None,
    temperature: float | None = None,
    max_new_tokens: int | None = None,
) -> str:
    """Run the configured vision-language model over in-memory images."""

    if not images:
        raise ModelRuntimeError("At least one image is required for vision inference.")
    app = _resolve_app(app)
    prepared = [_thumbnail(image) for image in images]
    conversation = _conversation_with_images(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        images=prepared,
    )
    return _generate_text(
        conversation,
        app=app,
        model_name=model_name,
        temperature=temperature,
        max_new_tokens=max_new_tokens,
    )


def _conversation_with_images(
    *,
    system_prompt: str,
    user_prompt: str,
    images: Sequence[Image.Image],
) -> list[dict[str, Any]]:
    image_parts = [{"type": "image", "image": image} for image in images]
    user_parts = image_parts + [{"type": "text", "text": user_prompt}]
    return [
        {"role": "system", "content": [{"type": "text", "text": system_prompt}]},
        {"role": "user", "content": user_parts},
    ]


def _conversation_from_history(
    *,
    system_prompt: str,
    history: Sequence[dict[str, str]],
    user_message: str,
) -> list[dict[str, Any]]:
    conversation: list[dict[str, Any]] = [
        {"role": "system", "content": [{"type": "text", "text": system_prompt}]}
    ]
    for message in history:
        role = message.get("role", "user")
        if role not in {"user", "assistant"}:
            role = "user"
        conversation.append(
            {"role": role, "content": [{"type": "text", "text": message.get("content", "")}]}  # type: ignore[arg-type]
        )
    conversation.append({"role": "user", "content": [{"type": "text", "text": user_message}]})
    return conversation


def _generate_text(
    conversation: list[dict[str, Any]],
    *,
    app: Flask,
    model_name: str | None = None,
    temperature: float | None = None,
    max_new_tokens: int | None = None,
) -> str:
    bundle = _load_vl_bundle(app, model_name=model_name)
    processor = bundle.processor
    model = bundle.model
    device = bundle.device
    cfg = app.config
    temperature = temperature if temperature is not None else float(cfg.get("VISION_MODEL_TEMPERATURE", 0.2))
    max_new_tokens = max_new_tokens or int(cfg.get("VISION_MODEL_MAX_NEW_TOKENS", 2048))

    try:
        raw_inputs = processor(conversation, return_tensors="pt")  # type: ignore[call-arg]
    except Exception as exc:  # pragma: no cover - depends on processor implementation
        raise ModelRuntimeError(f"Failed to build processor inputs: {exc}") from exc

    try:
        iterator = raw_inputs.items()  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - BatchEncoding supports items()
        raise ModelRuntimeError("Processor returned inputs without an items() interface") from None

    inputs: dict[str, Any] = {}
    for key, value in iterator:
        if isinstance(value, torch.Tensor):
            inputs[key] = value.to(device)
        else:
            inputs[key] = value

    pad_token_id = None
    tokenizer = bundle.tokenizer or getattr(processor, "tokenizer", None)
    if tokenizer is not None:
        pad_token_id = getattr(tokenizer, "pad_token_id", None) or getattr(tokenizer, "eos_token_id", None)
    if pad_token_id is None and hasattr(processor, "tokenizer"):
        pad_token_id = getattr(processor.tokenizer, "eos_token_id", None)

    do_sample = temperature > 0.0
    try:
        generated = model.generate(  # type: ignore[attr-defined]
            **inputs,
            max_new_tokens=max_new_tokens,
            temperature=temperature,
            do_sample=do_sample,
            pad_token_id=pad_token_id,
        )
    except Exception as exc:  # pragma: no cover - depends on runtime environment
        raise ModelRuntimeError(f"Local model generation failed: {exc}") from exc

    input_ids = inputs.get("input_ids")
    if input_ids is None:
        raise ModelRuntimeError("Processor inputs missing 'input_ids'; cannot determine completion tokens.")
    prefix_length = input_ids.shape[1]
    completion_tokens = generated[:, prefix_length:]

    decoder = getattr(processor, "batch_decode", None)
    if decoder is None:
        tokenizer = tokenizer or getattr(processor, "tokenizer", None)
        decoder = getattr(tokenizer, "batch_decode", None)
    if decoder is None:
        raise ModelRuntimeError("Processor does not expose a batch_decode method; unable to decode model output.")

    text = decoder(completion_tokens, skip_special_tokens=True)[0]
    return text.strip()


def parse_invoice(
    file_path: str,
    *,
    model_name: str | None = None,
    max_pages: int,
    app: Flask | None = None,
) -> Dict[str, Any]:
    app = _resolve_app(app)
    path = Path(file_path)
    images = _load_document_images(path, max_pages=max_pages, app=app)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.replace("{{MAX_PAGES}}", str(max_pages))
    user_prompt = "Analyse the attached invoice images and return JSON matching the schema."  # concise instruction
    conversation = _conversation_with_images(system_prompt=system_prompt, user_prompt=user_prompt, images=images)
    response_text = _generate_text(conversation, app=app, model_name=model_name, temperature=0.1)
    payload = _parse_json_payload(response_text)
    if payload is None:
        preview = response_text[:400].replace("\n", " ")
        raise ModelRuntimeError(f"Model response did not contain valid JSON payload: '{preview}'")
    return payload


def generate_file_analysis(
    *,
    file_path: str,
    system_prompt: str,
    analysis_prompt: str,
    model_name: str | None = None,
    max_pages: int,
    app: Flask | None = None,
) -> str:
    app = _resolve_app(app)
    path = Path(file_path)
    images = _load_document_images(path, max_pages=max_pages, app=app)
    conversation = _conversation_with_images(
        system_prompt=system_prompt,
        user_prompt=analysis_prompt,
        images=images,
    )
    return _generate_text(conversation, app=app, model_name=model_name, temperature=0.2)


def continue_chat(
    *,
    history: Sequence[dict[str, str]],
    user_message: str,
    system_prompt: str,
    model_name: str | None = None,
    app: Flask | None = None,
    temperature: float | None = None,
) -> str:
    app = _resolve_app(app)
    conversation = _conversation_from_history(system_prompt=system_prompt, history=history, user_message=user_message)
    return _generate_text(conversation, app=app, model_name=model_name, temperature=temperature)


def embed_text(
    text: str,
    *,
    model_name: str | None = None,
    app: Flask | None = None,
) -> list[float]:
    if not text:
        raise ValueError("Cannot embed empty text")
    app = _resolve_app(app)
    bundle = _load_embed_bundle(app, model_name=model_name)
    vector = bundle.model.encode([text], normalize_embeddings=True)[0]
    return [float(value) for value in vector]


def web_search(
    query: str,
    *,
    max_results: int,
) -> list[dict[str, str]]:
    _ensure_search_available()
    results: list[dict[str, str]] = []
    with DDGS() as ddgs:  # type: ignore[operator]
        for entry in ddgs.text(query, max_results=max_results):
            results.append(
                {
                    "title": str(entry.get("title", "")).strip(),
                    "snippet": str(entry.get("body", entry.get("snippet", ""))).strip(),
                    "url": str(entry.get("href", "")).strip(),
                }
            )
    return results


def healthcheck(app: Flask | None = None) -> Dict[str, Any]:
    app = _resolve_app(app)
    cfg = app.config
    model_name = cfg.get("VISION_MODEL_NAME", DEFAULT_VISION_MODEL)
    device_pref = cfg.get("VISION_MODEL_DEVICE", "auto")
    device = _resolve_device(device_pref)
    ready = True
    error: str | None = None
    try:
        _load_vl_bundle(app, model_name=model_name)
    except ModelRuntimeError as exc:
        ready = False
        error = str(exc)
    return {
        "model_name": model_name,
        "device": device,
        "ready": ready,
        "error": error,
    }


SYSTEM_PROMPT_TEMPLATE = """You are an expert invoice parser. Read the attached invoice (PDF/Image) and return STRICT JSON only.
Rules:
- Detect and normalize: invoice_no, invoice_date (ISO YYYY-MM-DD), vendor_gst, company_gst, currency (ISO 4217), subtotal, tax_total, grand_total.
- Extract line_items[] with: line_no, description_raw, hsn_sac (string or null), qty (number), unit_price (number), gst_rate (percent number), line_subtotal, line_tax, line_total, confidence (0–1).
- Include per_field_confidence between 0 and 1 for ALL header fields and EACH line item.
- If a value is missing or ambiguous, set it to null but still provide a reasonable confidence <= 0.5.
- Limit to the first {{MAX_PAGES}} pages if the file is longer.
- Derive advanced analysis covering duplicates, GST validation, HSN/SAC rate compliance, arithmetic checks, and AI-grounded market price benchmarking. Flag issues clearly.
- Estimate overall extraction accuracy (target >=100% when data quality permits).
- No extra commentary; respond with JSON ONLY matching this schema (use null instead of omitting fields when unknown):
{
    "header": {
        "invoice_no": "...", "invoice_date": "YYYY-MM-DD", "vendor_gst": "...", "company_gst": "...",
        "currency": "INR|USD|...", "subtotal": number|null, "tax_total": number|null, "grand_total": number|null,
        "per_field_confidence": {
            "invoice_no": 0.0-1.0, "invoice_date": 0.0-1.0, "vendor_gst": 0.0-1.0, "company_gst": 0.0-1.0,
            "currency": 0.0-1.0, "subtotal": 0.0-1.0, "tax_total": 0.0-1.0, "grand_total": 0.0-1.0
        }
    },
    "line_items": [
        {
            "line_no": 1, "description_raw": "...", "hsn_sac": "..."|null,
            "qty": number|null, "unit_price": number|null, "gst_rate": number|null,
            "line_subtotal": number|null, "line_tax": number|null, "line_total": number|null,
            "confidence": 0.0-1.0
        }
    ],
    "analysis": {
        "estimated_accuracy": 0.0-1.0|null,
        "duplicate_check": {
            "status": "clear|possible|flagged",
            "confidence": 0.0-1.0|null,
            "matches": [
                {"invoice_reference": "...", "similarity": 0.0-1.0|null, "reason": "..."}
            ],
            "rationale": "..."
        },
        "gst_validation": {
            "vendor": {"gst_number": "...", "valid": true|false|null, "confidence": 0.0-1.0|null, "source": "gst_portal|unverified|third_party", "detail": "..."},
            "company": {"gst_number": "...", "valid": true|false|null, "confidence": 0.0-1.0|null, "source": "gst_portal|unverified|third_party", "detail": "..."}
        },
        "hsn_rate_check": {
            "status": "aligned|mismatch|unknown",
            "confidence": 0.0-1.0|null,
            "violations": [
                {"line_no": 1, "billed_rate": number|null, "expected_rate": number|null, "description": "..."}
            ]
        },
        "arithmetic_check": {
            "passes": true|false|null,
            "confidence": 0.0-1.0|null,
            "discrepancies": [
                {"field": "subtotal", "expected": number|null, "actual": number|null, "difference": number|null, "note": "..."}
            ],
            "recomputed_totals": {"subtotal": number|null, "tax_total": number|null, "grand_total": number|null}
        },
        "price_outlier_check": {
            "confidence": 0.0-1.0|null,
            "method": "ai_grounding|historical|unknown",
            "outliers": [
                {"line_no": 1, "description": "...", "billed_price": number|null, "market_average": number|null, "delta_percent": number|null, "confidence": 0.0-1.0|null}
            ]
        }
    },
    "pages_parsed": number
}
"""


def _parse_json_payload(text_payload: str | None) -> Dict[str, Any] | None:
    if text_payload is None:
        return None
    trimmed = text_payload.strip()
    if not trimmed:
        return None
    try:
        return json.loads(trimmed)
    except json.JSONDecodeError:
        pass
    for candidate in _extract_json_candidates(trimmed):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    extracted = _scan_balanced_json(trimmed)
    if extracted is not None:
        try:
            return json.loads(extracted)
        except json.JSONDecodeError:
            return None
    return None


def _extract_json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    for match in _JSON_BLOCK.finditer(text):
        payload = match.group(1).strip()
        if payload:
            candidates.append(payload)
    return candidates


def _scan_balanced_json(text: str) -> str | None:
    depth = 0
    start_index = None
    for index, char in enumerate(text):
        if char == "{":
            if depth == 0:
                start_index = index
            depth += 1
        elif char == "}":
            if depth:
                depth -= 1
                if depth == 0 and start_index is not None:
                    snippet = text[start_index : index + 1].strip()
                    if snippet:
                        return snippet
                    start_index = None
    return None


__all__ = [
    "ModelRuntimeError",
    "parse_invoice",
    "generate_file_analysis",
    "continue_chat",
    "embed_text",
    "web_search",
    "healthcheck",
    "generate_from_images",
]
