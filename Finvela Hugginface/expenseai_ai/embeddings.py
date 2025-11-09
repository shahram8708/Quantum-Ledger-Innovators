"""Embedding utilities for normalized invoice item descriptions."""
from __future__ import annotations

import hashlib
import math

from flask import current_app

from expenseai_ai import model_client, norm
from expenseai_ext.db import db
from expenseai_models.item_embedding import ItemEmbedding


def normalize_for_embedding(text: str) -> str:
    """Normalize text prior to embedding, ensuring deterministic hashing."""
    normalized = norm.normalize_description(text)
    if not normalized:
        # Fallback to raw trimmed text to avoid empty lookups downstream.
        normalized = (text or "").strip().lower()
    return normalized


def embed_text(text: str, *, force_remote: bool = False) -> list[float]:
    """Return an embedding vector for the supplied text."""
    normalized = normalize_for_embedding(text)
    if not normalized:
        raise ValueError("Cannot embed empty text")

    app = current_app._get_current_object()

    model_name = app.config.get("EMBEDDING_MODEL_NAME")
    fallback_dims = int(app.config.get("EMBEDDING_FALLBACK_DIMS", 64))
    if app.config.get("EMBEDDING_DISABLE_REMOTE", False) and not force_remote:
        app.logger.debug(
            "Remote embeddings disabled; using fallback",
            extra={"text_preview": (normalized[:120] + "…") if len(normalized) > 120 else normalized},
        )
        return _fallback_embedding(normalized, fallback_dims)

    truncated = (normalized[:120] + "…") if len(normalized) > 120 else normalized
    try:
        vector = model_client.embed_text(normalized, model_name=model_name, app=app)
        app.logger.debug(
            "Generated embedding",
            extra={"text_preview": truncated, "length": len(vector)},
        )
        return vector
    except Exception as exc:  # pragma: no cover - defensive fallback
        app.logger.warning(
            "Embedding generation failed; using fallback",
            extra={"text_preview": truncated, "error": str(exc)},
        )
        return _fallback_embedding(normalized, fallback_dims)


def text_hash(text_norm: str) -> str:
    """Return a deterministic SHA256 hash for the normalized text."""
    digest = hashlib.sha256(text_norm.encode("utf-8")).hexdigest()
    return digest


def get_or_create_item_embedding(text: str) -> ItemEmbedding:
    """Fetch a cached embedding or create a new one for the text."""
    normalized = normalize_for_embedding(text)
    if not normalized:
        raise ValueError("Cannot embed empty text")

    key = text_hash(normalized)
    record = ItemEmbedding.query.filter_by(hash_key=key).first()
    if record:
        return record

    vector = embed_text(normalized)
    record = ItemEmbedding(hash_key=key, text_norm=normalized, vector=vector)
    db.session.add(record)
    db.session.flush()
    return record


def _fallback_embedding(text: str, dimensions: int) -> list[float]:
    """Return a deterministic embedding using locality-sensitive hashing as fallback."""
    dims = max(16, dimensions)
    vector = [0.0] * dims
    if not text:
        return vector

    tokens = [token for token in text.split() if token]
    if not tokens:
        tokens = [text]

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for offset in range(0, len(digest), 4):
            chunk = int.from_bytes(digest[offset : offset + 4], "big", signed=False)
            idx = chunk % dims
            magnitude = ((chunk >> 8) & 0xFFFF) / 65535.0  # scale to [0,1]
            sign = 1.0 if (chunk & 1) == 0 else -1.0
            vector[idx] += sign * (0.5 + magnitude)

    norm = math.sqrt(sum(val * val for val in vector))
    if norm > 0:
        vector = [val / norm for val in vector]
    return vector


__all__ = [
    "embed_text",
    "text_hash",
    "get_or_create_item_embedding",
    "normalize_for_embedding",
]
