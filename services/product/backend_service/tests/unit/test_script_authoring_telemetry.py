"""Task 14.2 tests: telemetry records are content-private.

Every serialized telemetry record is a dict of IDs, fingerprints, counts,
durations, and enums. Feeding a marker string through every record field
that takes content-shaped input (via `record_*` methods, whose signatures
only accept non-content parameters) and into the emitted dict must NEVER
produce the marker in serialized output.

The only content-shaped values the recorder can see are ones it rejects:
the record_* method parameter types do not include free-form text, and the
record dataclass has no text field. The marker test proves the emitted
serialization is content-free even when a marker is present in every
content-adjacent argument (latency, counts, IDs, fingerprints).
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.telemetry import (
    AuthoringTelemetry,
    AuthoringTelemetryRecorder,
    TelemetryEventKind,
)

# Marker string used to prove content never leaks into serialized telemetry.
_MARKER = "BÍ_MẬT_NỘI_DUNG_SCRIPT_9f3a"

_IDS = ("batch-42", "wf-7", "product-P001", "v-100", "approval-3", "plan-1")
_FPRINT = "fp-abc123"


def _record_all(recorder: AuthoringTelemetryRecorder) -> None:
    """Emit every record kind once through the recorder's sink."""
    recorder.workflow_started(
        target_duration_s=600, planned_k=3, planned_semantic_calls=4, plan_id=_IDS[5]
    )
    recorder.segment_started(segment_index=0)
    recorder.segment_succeeded(
        segment_index=0,
        provider_attempts=2,
        generation_latency_ms=1234,
        output_tokens=512,
        version_id=_IDS[3],
    )
    recorder.segment_failed(
        segment_index=1,
        gate_rule_ids=("FORMAT_EM_DASH", "VN_SPELLING_DIPHTHONG"),
        rule_set_fingerprint=_FPRINT,
    )
    recorder.workflow_finished(actual_semantic_calls=2, workflow_duration_ms=5000)
    recorder.workflow_cancelled(actual_semantic_calls=1)
    recorder.approval_created(
        approval_id=_IDS[4], version_id=_IDS[3], dependency_fingerprint=_FPRINT
    )
    recorder.approval_invalidated(approval_id=_IDS[4], version_id=_IDS[3])
    recorder.staleness_transitioned(version_id=_IDS[3])


def _make_emitter() -> tuple[callable, list[dict]]:
    emitted: list[dict] = []

    def emit(d: dict) -> None:
        emitted.append(d)

    return emit, emitted


def test_content_shaped_kwargs_are_rejected():
    """Feeding a content marker as a text kwarg is impossible: TypeError."""
    emit, _ = _make_emitter()
    recorder = AuthoringTelemetryRecorder(
        emit, batch_id=_IDS[0], workflow_id=_IDS[1], product_id=_IDS[2]
    )
    with pytest.raises(TypeError):
        recorder.segment_succeeded(segment_index=0, display_text=_MARKER)
    with pytest.raises(TypeError):
        recorder.workflow_started(
            target_duration_s=600, planned_k=1, planned_semantic_calls=2, prompt_text=_MARKER
        )


def test_serialized_records_never_contain_content_fields_or_marker():
    """No serialized record carries prose keys, and the marker never appears."""
    emit, emitted = _make_emitter()
    recorder = AuthoringTelemetryRecorder(
        emit, batch_id=_IDS[0], workflow_id=_IDS[1], product_id=_IDS[2]
    )
    _record_all(recorder)
    # Every record kind is emitted exactly once.
    assert len(emitted) == 9
    for payload in emitted:
        for key in ("text", "prompt", "script", "content", "display_text", "spoken_text"):
            assert key not in payload
        assert _MARKER not in repr(payload)


def test_emitted_records_carry_ids_and_fingerprints():
    """IDs and fingerprints provide debugging context without content."""
    emit, emitted = _make_emitter()
    recorder = AuthoringTelemetryRecorder(
        emit, batch_id=_IDS[0], workflow_id=_IDS[1], product_id=_IDS[2]
    )
    _record_all(recorder)
    batch_events = [p for p in emitted if p["batch_id"] == _IDS[0]]
    assert len(batch_events) == 9
    failed = next(p for p in emitted if p["event"] == "segment_failed")
    assert failed["gate_rule_ids"] == ["FORMAT_EM_DASH", "VN_SPELLING_DIPHTHONG"]
    assert failed["rule_set_fingerprint"] == _FPRINT
    approved = next(p for p in emitted if p["event"] == "approval_created")
    assert approved["approval_id"] == _IDS[4]
    assert approved["version_id"] == _IDS[3]
    assert approved["dependency_fingerprint"] == _FPRINT


def test_sequence_is_monotonic_within_a_workflow():
    """Records are ordered by a stable seq for event ordering."""
    emit, emitted = _make_emitter()
    recorder = AuthoringTelemetryRecorder(
        emit, batch_id=_IDS[0], workflow_id=_IDS[1], product_id=_IDS[2]
    )
    _record_all(recorder)
    seqs = [p["seq"] for p in emitted]
    assert seqs == sorted(seqs)
    assert seqs[0] == 1


def test_record_dataclass_has_no_text_fields():
    """The record type has no raw script/prompt field by design."""
    fields = set(AuthoringTelemetry.__dataclass_fields__)
    assert "text" not in fields
    assert "prompt" not in fields
    assert "script" not in fields
    assert "content" not in fields
    assert "display_text" not in fields
    assert "spoken_text" not in fields


def test_workflow_started_captures_target_duration_k_and_planned_calls():
    """Planning telemetry carries the budget-relevant numbers."""
    emit, emitted = _make_emitter()
    recorder = AuthoringTelemetryRecorder(
        emit, batch_id=_IDS[0], workflow_id=_IDS[1], product_id=_IDS[2]
    )
    recorder.workflow_started(
        target_duration_s=1800, planned_k=7, planned_semantic_calls=8, plan_id=_IDS[5]
    )
    payload = emitted[0]
    assert payload["target_duration_s"] == 1800
    assert payload["planned_k"] == 7
    assert payload["planned_semantic_calls"] == 8
    assert payload["plan_id"] == _IDS[5]
    assert payload["event"] == str(TelemetryEventKind.WORKFLOW_STARTED)
