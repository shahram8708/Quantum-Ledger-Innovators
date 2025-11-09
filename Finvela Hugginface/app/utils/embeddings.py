"""Embedding and duplicate detection utilities.

In this prototype we simulate embeddings using deterministic hashing.
In a production system you would call the local vision model or another
embedding system to generate high‑dimensional vectors and index them
with FAISS or Annoy.  Here, the `compute_embedding` function
produces a 64‑byte digest of the input text for demonstration.
"""

from __future__ import annotations

import hashlib
import numpy as np
import faiss  # type: ignore
from typing import List, Tuple


def compute_embedding(text: str) -> bytes:
    """Compute a pseudo‑embedding from text.

    Args:
        text: Input text.

    Returns:
        A 64‑byte digest representing the embedding.
    """
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return digest


def build_faiss_index(vectors: List[bytes]) -> faiss.IndexFlatL2:
    """Build a FAISS index from a list of byte vectors.

    Args:
        vectors: List of binary embeddings (must be of equal length).

    Returns:
        A FAISS IndexFlatL2 containing all vectors.
    """
    if not vectors:
        raise ValueError("No vectors provided to build index")
    dim = len(vectors[0])
    arr = np.array([np.frombuffer(v, dtype=np.uint8).astype(np.float32) for v in vectors])
    index = faiss.IndexFlatL2(dim)
    index.add(arr)
    return index


def search_similar(index: faiss.IndexFlatL2, query: bytes, k: int = 1) -> List[int]:
    """Search a FAISS index for the nearest neighbours of a query.

    Args:
        index: A FAISS index.
        query: The query embedding.
        k: Number of neighbours to return.

    Returns:
        List of indices of the top k nearest neighbours.
    """
    q = np.frombuffer(query, dtype=np.uint8).astype(np.float32)[None]
    distances, idx = index.search(q, k)
    return idx[0].tolist()