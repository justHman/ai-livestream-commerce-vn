"""Embedder — VN sentence embeddings for clustering + retrieval.

Primary: bkai-foundation-models/vietnamese-bi-encoder (Apache-2.0, 135M),
loaded via sentence-transformers. It is fine-tuned for semantic similarity —
do NOT use raw PhoBERT/ViDeBERTa [CLS] (poor for similarity).

Fallback: a deterministic hashing embedder (no model, no network) so the
Director logic is unit-testable offline and on CI. Same interface; lower
quality. Selected automatically when sentence-transformers / the model is
unavailable, or forced via DIRECTOR_EMBEDDER=hash.

Latency note: encoding short viewer comments with the 135M model is ~5-15ms
each on T4; batch-encoding a window is tens of ms — not the bottleneck (LLM/TTS
dominate).
"""

from __future__ import annotations

import hashlib
import math
import os
from typing import Optional, Sequence


def _l2_normalize(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity for already-finite vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


def average(vectors: list[list[float]]) -> list[float]:
    """L2-normalized centroid of a list of vectors."""
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            acc[i] += v[i]
    acc = [x / len(vectors) for x in acc]
    return _l2_normalize(acc)


class HashingEmbedder:
    """Deterministic, dependency-free embedder for offline tests.

    Hashes character n-grams into a fixed-dim bag-of-features vector, then
    L2-normalizes. Captures rough lexical overlap (enough to cluster obviously
    similar comments in tests) but is NOT semantic. Production uses the
    bi-encoder.
    """

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim
        self.name = "hashing-fallback"

    def _encode_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        t = text.lower().strip()
        tokens = t.split()
        # unigrams + bigrams
        grams = list(tokens) + [f"{a}_{b}" for a, b in zip(tokens, tokens[1:])]
        for g in grams:
            h = int(hashlib.md5(g.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        return _l2_normalize(vec)

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._encode_one(t) for t in texts]


class BiEncoderEmbedder:
    """sentence-transformers wrapper around the VN bi-encoder."""

    def __init__(self, model_id: str) -> None:
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(model_id)
        self.name = model_id

    def encode(self, texts: list[str]) -> list[list[float]]:
        embs = self.model.encode(texts, normalize_embeddings=True)
        return [list(map(float, e)) for e in embs]


def build_embedder(model_id: Optional[str] = None):
    """Return the best available embedder.

    Order: explicit hash override -> bi-encoder -> hashing fallback.
    """
    if os.environ.get("DIRECTOR_EMBEDDER", "").lower() == "hash":
        return HashingEmbedder()

    model_id = model_id or os.environ.get(
        "DIRECTOR_EMBEDDER_MODEL", "bkai-foundation-models/vietnamese-bi-encoder"
    )
    try:
        return BiEncoderEmbedder(model_id)
    except Exception as exc:  # sentence-transformers missing or offline
        print(f"[director] bi-encoder unavailable ({type(exc).__name__}); "
              "using hashing fallback. Set DIRECTOR_EMBEDDER_MODEL + install "
              "sentence-transformers for production quality.")
        return HashingEmbedder()
