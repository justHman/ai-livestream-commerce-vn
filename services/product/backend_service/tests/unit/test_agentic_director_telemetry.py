"""Offline tests for execution telemetry (task 12.10)."""

from __future__ import annotations

from backend.application.agentic_director.telemetry import (
    ExecutionTelemetry,
    InMemoryMetricSink,
    MetricSink,
    build_execution_telemetry,
)


def test_build_aggregates_exact_metric_counts():
    telemetry = build_execution_telemetry(
        "complex",
        {
            "llm_calls": 2,
            "prompt_tokens": 1500,
            "generated_tokens": 420,
            "evidence_rounds": 3,
            "evidence_cache_hits": 4,
            "evidence_cache_misses": 1,
            "latency_ms": 1234.6,
        },
        "answer",
    )
    assert telemetry.llm_calls == 2
    assert telemetry.prompt_tokens == 1500
    assert telemetry.generated_tokens == 420
    assert telemetry.evidence_rounds == 3
    assert telemetry.evidence_cache_hits == 4
    assert telemetry.evidence_cache_misses == 1
    assert telemetry.latency_ms == 1235


def test_build_ignores_unknown_metrics_and_missing_latency():
    telemetry = build_execution_telemetry(
        "factual_fast", {"llm_calls": 1, "unknown_metric": 99}, "answer"
    )
    assert telemetry.llm_calls == 1
    assert telemetry.latency_ms == 0


def test_to_metadata_has_no_text_content_keys():
    telemetry = build_execution_telemetry(
        "complex",
        {
            "llm_calls": 2,
            "prompt_tokens": 1500,
            "generated_tokens": 420,
            "evidence_rounds": 3,
            "evidence_cache_hits": 4,
            "evidence_cache_misses": 1,
            "latency_ms": 1234,
        },
        "answer",
    )
    metadata = telemetry.to_metadata()
    assert not {"text", "content", "viewer"} & metadata.keys()


def test_to_metadata_has_only_int_and_str_values():
    metadata = build_execution_telemetry(
        "complex",
        {
            "llm_calls": 2,
            "prompt_tokens": 1500,
            "generated_tokens": 420,
            "evidence_rounds": 3,
            "evidence_cache_hits": 4,
            "evidence_cache_misses": 1,
            "latency_ms": 1234,
        },
        "answer",
    ).to_metadata()
    assert all(isinstance(v, (int, str)) for v in metadata.values())


def test_to_metadata_preserves_path_and_terminal():
    metadata = ExecutionTelemetry(path="factual_fast", terminal="answer").to_metadata()
    assert metadata["execution_path"] == "factual_fast"
    assert metadata["terminal"] == "answer"


def test_metric_sink_matches_callable_shape():
    assert isinstance(InMemoryMetricSink(), MetricSink)


def test_in_memory_sink_records_ordered_pairs():
    sink = InMemoryMetricSink()
    sink.record("llm_calls", 1)
    sink.record("latency_ms", 250.5)
    sink.record("evidence_cache_hits", 3)
    assert sink.records == [("llm_calls", 1), ("latency_ms", 250.5), ("evidence_cache_hits", 3)]
