"""MetricsRegistry: bounded-label counters, gauges, fixed-bucket histograms
(Change T tasks 12.1-12.3).

The registry is the self-managed metrics mechanism (cluster 8 decision: no
prometheus client — JSON snapshot via ``/v1/audio/metrics`` is enough for
this change). Label values are bounded by construction: ``incr_request``
accepts only the fixed outcome/priority sets, and identity values never
become labels.
"""

from __future__ import annotations

import pytest

from tts.observability.metrics import (
    BATCH_SIZE_BUCKETS,
    WAIT_MS_BUCKETS,
    MetricsRegistry,
    record_batch,
    record_enrollment,
    record_queue_wait,
)


def test_incr_request_counts_by_bounded_labels() -> None:
    registry = MetricsRegistry()
    registry.incr_request("admitted", "normal", "fake")
    registry.incr_request("admitted", "normal", "fake")
    registry.incr_request("completed", "high", "fake")
    snapshot = registry.snapshot()
    assert snapshot["requests"]["admitted.normal.fake"] == 2
    assert snapshot["requests"]["completed.high.fake"] == 1


def test_incr_request_rejects_unknown_labels() -> None:
    registry = MetricsRegistry()
    registry.incr_request("mystery_outcome", "normal", "fake")
    registry.incr_request("admitted", "supreme", "fake")
    assert registry.snapshot()["requests"] == {}


def test_observe_records_bucket_and_total() -> None:
    registry = MetricsRegistry()
    registry.observe("queue_wait_ms", 12.0, WAIT_MS_BUCKETS)
    registry.observe("queue_wait_ms", 9999.0, WAIT_MS_BUCKETS)
    hist = registry.snapshot()["histograms"]["queue_wait_ms"]
    assert hist["buckets"]["25"] == 1
    assert hist["buckets"]["inf"] == 1
    assert hist["total"] == 2


def test_gauges_and_counters_are_independent() -> None:
    registry = MetricsRegistry()
    registry.incr("batches", 3)
    registry.gauge("pending_depth", 7)
    snapshot = registry.snapshot()
    assert snapshot["counters"]["batches"] == 3
    assert snapshot["gauges"]["pending_depth"] == 7


def test_record_batch_records_fill_wall_and_rtf() -> None:
    registry = MetricsRegistry()
    record_batch(
        registry,
        batch_size=4,
        max_batch_size=32,
        inference_started=__import__("time").monotonic() - 2.0,
        audio_seconds=10.0,
    )
    snapshot = registry.snapshot()
    assert snapshot["histograms"]["batch_size"]["buckets"]["4"] == 1
    assert snapshot["histograms"]["batch_fill_ratio"]["buckets"]["0.25"] == 1
    assert snapshot["gauges"]["audio_seconds_total"] == 10.0
    assert snapshot["gauges"]["rtf"] == pytest.approx(5.0, rel=1e-3)
    assert snapshot["gauges"]["audio_seconds_per_wall_second"] == pytest.approx(5.0, rel=1e-3)


def test_record_queue_wait_with_none_registry_is_noop() -> None:
    record_queue_wait(None, 0.0)
    record_batch(None, batch_size=1, max_batch_size=1, inference_started=0.0, audio_seconds=1.0)


def test_record_enrollment_counts_success_and_failure() -> None:
    registry = MetricsRegistry()
    started = __import__("time").monotonic()
    record_enrollment(registry, started, succeeded=True)
    record_enrollment(registry, started, succeeded=False)
    snapshot = registry.snapshot()
    assert snapshot["counters"]["voice_enrollments_total"] == 1
    assert snapshot["counters"]["voice_enrollments_failed_total"] == 1


def test_snapshot_is_immutable_json_safe() -> None:
    registry = MetricsRegistry()
    registry.incr("batches", 1)
    snapshot = registry.snapshot()
    snapshot["counters"]["batches"] = 99
    assert registry.snapshot()["counters"]["batches"] == 1
    assert isinstance(registry.snapshot()["histograms"], dict)
    assert isinstance(registry.snapshot()["gauges"], dict)


def test_default_registry_is_singleton() -> None:
    from tts.observability.metrics import get_metrics_registry

    assert get_metrics_registry() is get_metrics_registry()


@pytest.mark.parametrize(
    "name",
    [
        "batch_size",
        "batch_fill_ratio",
        "provider_inference_ms",
        "queue_wait_ms",
        "coalescing_wait_ms",
    ],
)
def test_histogram_bucket_edges_are_registered(name: str) -> None:
    registry = MetricsRegistry()
    registry.observe(name, 1.0, BATCH_SIZE_BUCKETS)
    assert name in registry.snapshot()["histograms"]
