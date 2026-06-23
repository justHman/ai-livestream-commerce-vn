"""Director config — dashboard-admin-writable knobs (challenges 2 & 3).

All thresholds the live-commerce operator tunes from the admin dashboard live
here. Nothing is hardcoded in the FSM; the dashboard writes a StreamConfig
(or it loads from JSON/env), keeping the orchestration policy data-driven and
portable (same object Colab -> AWS).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class StreamConfig:
    """Tunable orchestration policy."""

    # Phase: opening (challenge 2)
    opening_timeout_sec: float = 75.0      # opening lasts up to this long
    viewer_threshold: int = 5              # ...or until viewers exceed this (whichever first)

    # Phase: product switching (challenge 3) — OR of these conditions
    product_time_budget_sec: float = 360.0   # hard budget per product (5-8 min typical)
    engagement_decay_sec: float = 45.0       # no relevant msg this long -> engagement cap
    max_clusters_per_product: int = 4        # hard cap on Q&A clusters per product

    # Traffic mode (challenge 1) — msgs/sec thresholds
    traffic_low_threshold: float = 0.2
    traffic_high_threshold: float = 2.0

    # Cluster scoring weights (challenge 5)
    w_phase_relevance: float = 1.0
    w_intent: float = 0.8
    w_cluster_size: float = 0.5
    w_recency: float = 0.6
    recency_half_life_sec: float = 30.0      # recency decays with this half-life

    # Cluster selection window (challenge 5): wider than ±5s, not whole history
    selection_window_sec: float = 75.0
    cluster_max_skips: int = 3               # cluster skipped this many times -> drop
    cluster_max_age_sec: float = 120.0       # cluster older than this -> drop

    # Barge-in (interrupt) gate: only clusters scoring above this may interrupt
    interrupt_score_threshold: float = 1.5

    # Online clustering similarity threshold (cosine)
    cluster_merge_threshold: float = 0.55

    @classmethod
    def from_json(cls, path: str | Path) -> "StreamConfig":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def to_json(self, path: Optional[str | Path] = None) -> str:
        s = json.dumps(asdict(self), ensure_ascii=False, indent=2)
        if path:
            Path(path).write_text(s, encoding="utf-8")
        return s
