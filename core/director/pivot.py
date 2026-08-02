"""Small deterministic demand-pivot helpers for Stage 2."""

from __future__ import annotations

from collections.abc import Iterable


def demand_share(product_id: str, product_ids: Iterable[str]) -> float:
    ids = list(product_ids)
    return ids.count(product_id) / len(ids) if ids else 0.0


def should_enter_pivot(
    product_id: str,
    product_ids: Iterable[str],
    *,
    min_comments: int = 5,
    enter_share: float = 0.60,
    score_margin: float = 0.15,
    top_score: float = 0.0,
    current_score: float = 0.0,
) -> bool:
    ids = list(product_ids)
    return (
        len(ids) >= min_comments
        and demand_share(product_id, ids) >= enter_share
        and top_score - current_score >= score_margin
    )


def should_exit_pivot(product_id: str, product_ids: Iterable[str], *, exit_share: float = 0.45) -> bool:
    return demand_share(product_id, product_ids) < exit_share
