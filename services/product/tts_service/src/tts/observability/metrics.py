"""Self-managed process metrics registry (Change T task 12).

Decision (cluster 8): no prometheus client — this service is scraped nowhere
and the change forbids new deps unless forced. The registry is a small
thread-safe counters/histograms store with a JSON snapshot. All label values
are bounded (provider/backend/priority/outcome); unbounded identity values
(session/request/voice-profile ids) never become labels — they are the
leakage source task 12.8 guards against.

Histograms are fixed-bucket counts (no quantile math); gauges are plain
last-write floats. ``record_*`` helpers never raise so hot paths cannot
break on a metrics bug.
"""

from __future__ import annotations

import threading
import time
from collections import Counter, defaultdict
from typing import Optional

# Histogram bucket edges (upper bound inclusive, milliseconds).
WAIT_MS_BUCKETS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, float("inf"))
BATCH_SIZE_BUCKETS = (1, 2, 4, 8, 16, 32, 64, float("inf"))
FILL_RATIO_BUCKETS = (0.25, 0.5, 0.75, 0.9, 1.0, float("inf"))
INFERENCE_MS_BUCKETS = (50, 100, 250, 500, 1000, 2000, 5000, 10000, float("inf"))
ENROLL_MS_BUCKETS = (500, 1000, 2000, 5000, 10000, 30000, float("inf"))

OUTCOMES = frozenset(
    {"admitted", "completed", "rejected", "deadline", "cancelled", "provider_failed"}
)
PRIORITIES = ("normal", "high")


def _bucketed(counts: Counter, edges: tuple[float, ...]) -> dict:
    """Render fixed-bucket counts as {upper_bound: count} with a total."""
    return {"buckets": {str(edge): counts[edge] for edge in edges}, "total": sum(counts.values())}


class MetricsRegistry:
    """Thread-safe counters, gauges, and fixed-bucket histograms."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: Counter = Counter()
        # outcome x priority x provider -> counter key.
        self._requests: Counter = Counter()
        self._gauges: dict[str, float] = {}
        self._histograms: dict[str, Counter] = defaultdict(Counter)
        self._histogram_edges: dict[str, tuple[float, ...]] = {}

    # ── counters ─────────────────────────────────────────────────────────────
    def incr(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def incr_request(self, outcome: str, priority: str, provider: str) -> None:
        """Count one synthesis request by bounded labels only."""
        if outcome not in OUTCOMES or priority not in PRIORITIES:
            return
        with self._lock:
            self._requests[(outcome, priority, provider)] += 1

    # ── gauges ───────────────────────────────────────────────────────────────
    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def gauge_add(self, name: str, delta: float) -> None:
        with self._lock:
            self._gauges[name] = self._gauges.get(name, 0.0) + delta

    # ── histograms ───────────────────────────────────────────────────────────
    def observe(self, name: str, value_ms: float, edges: tuple[float, ...]) -> None:
        bucket = next(edge for edge in edges if value_ms <= edge)
        with self._lock:
            self._histograms[name][bucket] += 1
            self._histogram_edges[name] = edges

    def snapshot(self) -> dict:
        """Immutable JSON-safe snapshot for the metrics endpoint."""
        with self._lock:
            requests = {
                f"{outcome}.{priority}.{provider}": count
                for (outcome, priority, provider), count in sorted(self._requests.items())
            }
            histograms = {
                name: _bucketed(counts, self._histogram_edges[name])
                for name, counts in sorted(self._histograms.items())
            }
            return {
                "counters": dict(sorted(self._counters.items())),
                "requests": requests,
                "gauges": dict(sorted(self._gauges.items())),
                "histograms": histograms,
            }


_default_registry = MetricsRegistry()


def get_metrics_registry() -> MetricsRegistry:
    """Return the process-global registry."""
    return _default_registry


def _record_wait(
    registry: MetricsRegistry, name: str, edges: tuple[float, ...], started: float
) -> None:
    registry.observe(name, (time.monotonic() - started) * 1000.0, edges)


def record_queue_wait(registry: Optional[MetricsRegistry], started: float) -> None:
    if registry is not None:
        _record_wait(registry, "queue_wait_ms", WAIT_MS_BUCKETS, started)


def record_coalescing_wait(registry: Optional[MetricsRegistry], started: float) -> None:
    if registry is not None:
        _record_wait(registry, "coalescing_wait_ms", WAIT_MS_BUCKETS, started)


def record_batch(
    registry: Optional[MetricsRegistry],
    *,
    batch_size: int,
    max_batch_size: int,
    inference_started: float,
    audio_seconds: float,
) -> None:
    """Record one dispatched batch (task 12.3): size, fill, wall, RTF."""
    if registry is None:
        return
    registry.observe("batch_size", batch_size, BATCH_SIZE_BUCKETS)
    registry.observe(
        "batch_fill_ratio",
        batch_size / max_batch_size if max_batch_size else 0.0,
        FILL_RATIO_BUCKETS,
    )
    wall_seconds = time.monotonic() - inference_started
    registry.observe("provider_inference_ms", wall_seconds * 1000.0, INFERENCE_MS_BUCKETS)
    registry.gauge_add("audio_seconds_total", audio_seconds)
    if wall_seconds > 0:
        registry.gauge("rtf", audio_seconds / wall_seconds)
        registry.gauge("audio_seconds_per_wall_second", audio_seconds / wall_seconds)


def record_enrollment(registry: Optional[MetricsRegistry], started: float, succeeded: bool) -> None:
    if registry is None:
        return
    registry.observe("enrollment_ms", (time.monotonic() - started) * 1000.0, ENROLL_MS_BUCKETS)
    registry.incr("voice_enrollments_total" if succeeded else "voice_enrollments_failed_total")
