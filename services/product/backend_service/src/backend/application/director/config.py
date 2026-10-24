"""Director config — dashboard-admin-writable knobs (challenges 2 & 3).

All thresholds the live-commerce operator tunes from the admin dashboard live
here. Nothing is hardcoded in the FSM; the dashboard writes a StreamConfig
(or it loads from JSON/env), keeping the orchestration policy data-driven and
portable (same object Colab -> AWS).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class StreamConfig:
    """Tunable orchestration policy."""

    # Phase: opening (challenge 2)
    opening_timeout_sec: float = 75.0  # opening lasts up to this long
    viewer_threshold: int = 5  # ...or until viewers exceed this (whichever first)

    # Phase: product switching (challenge 3) — OR of these conditions
    product_time_budget_sec: float = 360.0  # hard budget per product (5-8 min typical)
    engagement_decay_sec: float = 45.0  # no relevant msg this long -> engagement cap
    max_clusters_per_product: int = 4  # hard cap on Q&A clusters per product

    # Traffic mode (challenge 1) — msgs/sec thresholds
    traffic_low_threshold: float = 0.2
    traffic_high_threshold: float = 2.0

    # Cluster scoring weights (challenge 5)
    w_phase_relevance: float = 1.0
    w_intent: float = 0.8
    w_cluster_size: float = 0.5
    w_recency: float = 0.6
    recency_half_life_sec: float = 30.0  # recency decays with this half-life

    # Cluster selection window (challenge 5): wider than ±5s, not whole history
    selection_window_sec: float = 75.0
    cluster_max_skips: int = 3  # cluster skipped this many times -> drop
    cluster_max_age_sec: float = 120.0  # cluster older than this -> drop

    # Barge-in (interrupt) gate: only clusters scoring above this may interrupt
    interrupt_score_threshold: float = 1.5

    # Online clustering similarity threshold (cosine)
    cluster_merge_threshold: float = 0.375

    # Stage 2 Q&A window and prepared pipeline controls
    max_qa_clusters_per_window: int = 2
    qa_window_hard_timeout_sec: float = 45.0
    qa_topic_cooldown_sec: float = 120.0
    # 'unknown' intent lumps unrelated comments together: short cooldown, and any new
    # comment counts as new content (see decision.py).
    qa_unknown_topic_cooldown_sec: float = 15.0
    answer_cache_variants: int = 3
    prepared_turn_depth: int = 3
    transient_retry_count: int = 1
    demand_pivot_enter_share: float = 0.60
    demand_pivot_exit_share: float = 0.45
    demand_pivot_min_comments: int = 5
    demand_pivot_score_margin: float = 0.15
    # Smallest cluster of similar comments the Director answers (high-value intents always
    # qualify). 2 keeps a single stray comment from interrupting the script on a busy stream;
    # on a small live every viewer matters, so 1 answers each question.
    qa_min_cluster_size: int = 2
    # Questions the Director may still answer on the LAST product after its time budget or cluster cap would have
    # switched to the closing. Bounded so a busy stream cannot postpone the closing forever.
    max_closing_qa_answers: int = 3

    def validate_runtime(self) -> None:
        if not 0.2 <= self.traffic_high_threshold <= 5.0:
            raise ValueError("traffic_high_threshold must be between 0.2 and 5")
        if self.max_qa_clusters_per_window < 1 or self.qa_window_hard_timeout_sec <= 0:
            raise ValueError("Q&A window limits must be positive")
        if self.answer_cache_variants < 1 or self.prepared_turn_depth < 1:
            raise ValueError("cache variants and prepared depth must be positive")
        if not 0 < self.demand_pivot_exit_share < self.demand_pivot_enter_share <= 1:
            raise ValueError("pivot shares must satisfy 0 < exit < enter <= 1")
        if self.demand_pivot_min_comments < 1 or self.demand_pivot_score_margin < 0:
            raise ValueError("pivot gates must be non-negative")
        if self.qa_min_cluster_size < 1:
            raise ValueError("qa_min_cluster_size must be at least 1")
        if self.max_closing_qa_answers < 0:
            raise ValueError("max_closing_qa_answers must not be negative")

    @classmethod
    def from_env(cls) -> "StreamConfig":
        """Defaults with the deployment's overrides (DIRECTOR_QA_MIN_CLUSTER_SIZE,
        DIRECTOR_PREPARED_TURN_DEPTH)."""
        raw = os.environ.get("DIRECTOR_QA_MIN_CLUSTER_SIZE", "").strip()
        try:
            size = int(raw) if raw else cls.qa_min_cluster_size
        except ValueError:
            size = cls.qa_min_cluster_size
        raw_depth = os.environ.get("DIRECTOR_PREPARED_TURN_DEPTH", "").strip()
        try:
            depth = int(raw_depth) if raw_depth else 1
        except ValueError:
            depth = 1
        # Approved units are prepared in milliseconds, so one turn ahead hides no latency; a
        # deeper queue only delays a viewer's answer behind several script units.
        return cls(qa_min_cluster_size=max(1, size), prepared_turn_depth=max(1, depth))

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
