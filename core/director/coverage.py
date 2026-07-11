"""Director semantic coverage — mark key_selling_points covered by speech.

After each utterance, embed speech and cosine-match against product
key_selling_points (threshold default 0.75). Deterministic, auditable; does
not trust LLM self-report for product-switch financial decisions.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from .embedder import cosine


def mark_coverage(
    embedder,
    speech: str,
    key_points: Sequence[str],
    *,
    threshold: float = 0.75,
    already_covered: Iterable[str] | None = None,
) -> set[str]:
    """Return the set of key points covered by ``speech`` (incl. already_covered).

    Uses embedder.encode([speech, *key_points]) then cosine match.
    Empty speech or empty key_points → only already_covered.
    """
    covered: set[str] = set(already_covered or ())
    points = [p for p in key_points if p and str(p).strip()]
    text = (speech or "").strip()
    if not text or not points:
        return covered

    vectors = embedder.encode([text, *points])
    if not vectors or len(vectors) < 1 + len(points):
        return covered
    speech_vec = vectors[0]
    for i, point in enumerate(points):
        if point in covered:
            continue
        score = cosine(speech_vec, vectors[i + 1])
        if score >= threshold:
            covered.add(point)
    return covered


def coverage_ratio(covered: set[str] | None, key_points: Sequence[str]) -> float:
    """Fraction of key_points covered in [0, 1]. Empty key_points → 1.0."""
    points = [p for p in key_points if p and str(p).strip()]
    if not points:
        return 1.0
    cov = covered or set()
    return len([p for p in points if p in cov]) / len(points)


__all__ = ["mark_coverage", "coverage_ratio"]
