"""Content-free chunk-decision telemetry tests (tasks 7.1, 7.4).

Every assertion runs through the real ``TextChunker`` emit path; the
collector records only content-free fields and never the chunk text.
"""

from __future__ import annotations

import pytest

from backend.application.speech_chunking.duration import SpeechDurationEstimator
from backend.application.speech_chunking.telemetry import ChunkTelemetry, TelemetryCollector
from backend.application.text_chunker import TextChunker

PHRASE = "Chào bạn. Hôm nay shop giảm giá 50%, nhanh tay nhé!"


def _fixed_chunker(telemetry: TelemetryCollector, **kwargs: object) -> TextChunker:
    return TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=4,
        target_chars=20,
        max_chars=40,
        policy="fixed",
        telemetry=telemetry,
        **kwargs,
    )


def test_emit_records_content_free_fields_fixed_policy() -> None:
    telemetry = TelemetryCollector()
    chunker = _fixed_chunker(telemetry)
    chunker.feed(PHRASE)
    records = telemetry.records
    assert records, "feeding a completed phrase must record a chunk decision"
    record = records[0]
    assert record.seq == 0
    assert record.decision_reason == "punctuation"
    assert record.char_length == len(PHRASE.split(".")[0]) + 1
    assert record.hard_max_used is False
    assert record.protected_span_fallback is False
    assert record.policy == "fixed"
    assert record.is_final is False
    assert not hasattr(record, "text")
    assert PHRASE not in repr(record)


def test_finalize_records_final_flag_and_reason() -> None:
    telemetry = TelemetryCollector()
    chunker = _fixed_chunker(telemetry)
    chunker.feed("mua ngay")
    chunker.finalize()
    records = telemetry.records
    assert records[-1].is_final is True
    assert records[-1].decision_reason == "finalize"


def test_hard_max_records_hard_max_flag() -> None:
    telemetry = TelemetryCollector()
    chunker = _fixed_chunker(telemetry)
    chunker.feed("a" * 100)
    records = [r for r in telemetry.records if r.decision_reason == "hard_max"]
    assert records, "an oversized buffer must record a hard_max decision"
    assert all(r.hard_max_used for r in records)
    assert all(r.char_length <= 40 for r in records)


def test_latency_deadline_flush_records_reason() -> None:
    fake_clock = iter([0.0, 10.0])
    telemetry = TelemetryCollector()
    chunker = _fixed_chunker(telemetry, flush_timeout_ms=5, clock=lambda: next(fake_clock))
    chunker.feed("đủ dài chữ")  # >= min_chars, buffer_started_at=0.0
    chunker.check_timeout()  # clock=10.0 -> deadline fired
    records = telemetry.records
    assert any(r.decision_reason == "latency_deadline" for r in records)


def test_adaptive_forced_protected_split_stamps_protected_fallback() -> None:
    telemetry = TelemetryCollector()
    chunker = TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=4,
        target_chars=20,
        max_chars=40,
        policy="adaptive_vi",
        telemetry=telemetry,
    )
    # A balanced-paren region protects interior punctuation AND whitespace:
    # no safe boundary exists inside it, so the cap forces a split cutting
    # the protected span (interior punctuation of the region).
    chunker.feed("Mua (" + "199.000đ, " * 5 + "giá 50%,)")
    records = telemetry.records
    assert records, "the oversized buffer must be drained"
    assert any(r.protected_span_fallback for r in records), (
        "a forced cap split inside a protected span must be flagged"
    )


def test_collector_never_stores_text() -> None:
    telemetry = TelemetryCollector()
    chunker = _fixed_chunker(telemetry)
    chunker.feed(PHRASE)
    chunker.finalize()
    assert telemetry.records
    assert PHRASE not in repr(telemetry)
    assert PHRASE not in repr(telemetry.records)
    assert PHRASE not in repr(telemetry.records[0])


def test_estimator_duration_recorded_adaptive() -> None:
    estimator = SpeechDurationEstimator()
    telemetry = TelemetryCollector()
    chunker = TextChunker(
        session_id="s",
        utterance_id="u",
        min_chars=4,
        target_chars=20,
        max_chars=40,
        policy="adaptive_vi",
        estimator=estimator,
        telemetry=telemetry,
    )
    chunks = chunker.feed(PHRASE)
    assert chunks, "the phrase must drain under adaptive policy"
    expected = estimator.estimate_ms(chunks[0].text)
    assert telemetry.records[0].estimated_duration_ms == pytest.approx(expected)
    assert telemetry.records[0].estimated_duration_ms is not None


def test_estimator_not_required_fixed_policy_records_none_duration() -> None:
    telemetry = TelemetryCollector()
    chunker = _fixed_chunker(telemetry)
    chunker.feed(PHRASE)
    records = telemetry.records
    assert records
    assert all(r.estimated_duration_ms is None for r in records)


def test_telemetry_record_dataclass_shape() -> None:
    record = ChunkTelemetry(
        seq=0,
        decision_reason="hard_max",
        char_length=40,
        estimated_duration_ms=1234.5,
        hard_max_used=True,
        policy="adaptive_vi",
        is_final=False,
    )
    assert record.seq == 0
    assert record.decision_reason == "hard_max"
    assert record.char_length == 40
    assert record.estimated_duration_ms == pytest.approx(1234.5)
    assert record.hard_max_used is True
    assert record.protected_span_fallback is False
    assert record.policy == "adaptive_vi"
    assert record.is_final is False
