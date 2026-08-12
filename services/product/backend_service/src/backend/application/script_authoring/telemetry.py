"""Content-private authoring telemetry (task 14.1 / design Decision 21).

Every authoring event is recorded as a typed ``AuthoringTelemetry`` record
with stable ``*_id``/fingerprint fields for debugging and a monotonic
sequence number for ordering. Records are content-private BY CONSTRUCTION:
the record class has no script/prompt text field, and ``record_*`` methods
accept only IDs, counts, durations, and enums — never prose.

Emission is an injectable callable (``emit``) so callers can route records
to the observability collector, SSE, or a no-op in tests without this module
touching any logging/network machinery. This module is pure/lightweight:
no LLM, no network, no mutable global state.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import StrEnum
from typing import Callable, Optional

__all__ = [
    "AuthoringTelemetry",
    "TelemetryEventKind",
    "AuthoringTelemetryRecorder",
    "emit_callable",
]

# A record emitter: receives one serialized telemetry record. Injectable so
# the recorder stays free of logging/SSE/network dependencies.
emit_callable = Callable[[dict], None]


class TelemetryEventKind(StrEnum):
    """Stable telemetry event kinds — never free-form strings."""

    WORKFLOW_STARTED = "workflow_started"
    SEGMENT_STARTED = "segment_started"
    SEGMENT_SUCCEEDED = "segment_succeeded"
    SEGMENT_FAILED = "segment_failed"
    WORKFLOW_FINISHED = "workflow_finished"
    WORKFLOW_CANCELLED = "workflow_cancelled"
    APPROVAL_CREATED = "approval_created"
    APPROVAL_INVALIDATED = "approval_invalidated"
    STALENESS_TRANSITIONED = "staleness_transitioned"


@dataclass(frozen=True)
class AuthoringTelemetry:
    """One content-private authoring event record.

    Fields are limited to IDs, fingerprints, counts, durations, and enums.
    NO raw script/prompt text is stored — a record cannot leak prose even
    if a caller forgets to redact it.
    """

    event: TelemetryEventKind
    batch_id: str
    workflow_id: str
    product_id: str
    seq: int
    target_duration_s: int = 0
    planned_k: int = 0
    planned_semantic_calls: int = 0
    actual_semantic_calls: int = 0
    provider_attempts: int = 1
    segment_index: Optional[int] = None
    generation_latency_ms: Optional[int] = None
    output_tokens: Optional[int] = None
    gate_rule_ids: tuple[str, ...] = ()
    rule_set_fingerprint: str = ""
    workflow_duration_ms: Optional[int] = None
    cancelled: bool = False
    version_id: Optional[str] = None
    approval_id: Optional[str] = None
    dependency_fingerprint: str = ""
    plan_id: Optional[str] = None

    def to_dict(self) -> dict:
        """Deterministic serializable mapping (sorted keys, list-ified tuples)."""
        d = asdict(self)
        d["event"] = str(self.event)
        d["gate_rule_ids"] = list(self.gate_rule_ids)
        return {k: d[k] for k in sorted(d)}


class AuthoringTelemetryRecorder:
    """Typed content-private telemetry recorder with injectable emission.

    ``emit`` defaults to a no-op so the recorder is usable in any process
    without wiring; production callers inject the observability sink.
    """

    def __init__(
        self,
        emit: Optional[emit_callable] = None,
        *,
        batch_id: str = "",
        workflow_id: str = "",
        product_id: str = "",
    ) -> None:
        self._emit: emit_callable = emit or (lambda _d: None)
        self._batch_id = batch_id
        self._workflow_id = workflow_id
        self._product_id = product_id
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _record(self, event: TelemetryEventKind, **kw) -> AuthoringTelemetry:
        rec = AuthoringTelemetry(
            event=event,
            batch_id=self._batch_id,
            workflow_id=self._workflow_id,
            product_id=self._product_id,
            seq=self._next_seq(),
            **kw,
        )
        self._emit(rec.to_dict())
        return rec

    def workflow_started(
        self,
        *,
        target_duration_s: int,
        planned_k: int,
        planned_semantic_calls: int,
        plan_id: Optional[str] = None,
    ) -> AuthoringTelemetry:
        """Workflow planning completed for one product."""
        return self._record(
            TelemetryEventKind.WORKFLOW_STARTED,
            target_duration_s=target_duration_s,
            planned_k=planned_k,
            planned_semantic_calls=planned_semantic_calls,
            plan_id=plan_id,
        )

    def segment_started(self, *, segment_index: int) -> AuthoringTelemetry:
        """A fixed-index segment generation begins."""
        return self._record(TelemetryEventKind.SEGMENT_STARTED, segment_index=segment_index)

    def segment_succeeded(
        self,
        *,
        segment_index: int,
        provider_attempts: int = 1,
        generation_latency_ms: Optional[int] = None,
        output_tokens: Optional[int] = None,
        version_id: Optional[str] = None,
    ) -> AuthoringTelemetry:
        """Segment gate PASS after generation."""
        return self._record(
            TelemetryEventKind.SEGMENT_SUCCEEDED,
            segment_index=segment_index,
            provider_attempts=provider_attempts,
            generation_latency_ms=generation_latency_ms,
            output_tokens=output_tokens,
            version_id=version_id,
        )

    def segment_failed(
        self,
        *,
        segment_index: int,
        gate_rule_ids: tuple[str, ...] = (),
        rule_set_fingerprint: str = "",
    ) -> AuthoringTelemetry:
        """Segment gate FAIL; later segment calls are not scheduled."""
        return self._record(
            TelemetryEventKind.SEGMENT_FAILED,
            segment_index=segment_index,
            gate_rule_ids=tuple(gate_rule_ids),
            rule_set_fingerprint=rule_set_fingerprint,
        )

    def workflow_finished(
        self,
        *,
        actual_semantic_calls: int,
        workflow_duration_ms: Optional[int] = None,
    ) -> AuthoringTelemetry:
        return self._record(
            TelemetryEventKind.WORKFLOW_FINISHED,
            actual_semantic_calls=actual_semantic_calls,
            workflow_duration_ms=workflow_duration_ms,
        )

    def workflow_cancelled(self, *, actual_semantic_calls: int = 0) -> AuthoringTelemetry:
        return self._record(
            TelemetryEventKind.WORKFLOW_CANCELLED,
            actual_semantic_calls=actual_semantic_calls,
            cancelled=True,
        )

    def approval_created(
        self, *, approval_id: str, version_id: str, dependency_fingerprint: str
    ) -> AuthoringTelemetry:
        return self._record(
            TelemetryEventKind.APPROVAL_CREATED,
            approval_id=approval_id,
            version_id=version_id,
            dependency_fingerprint=dependency_fingerprint,
        )

    def approval_invalidated(self, *, approval_id: str, version_id: str) -> AuthoringTelemetry:
        return self._record(
            TelemetryEventKind.APPROVAL_INVALIDATED,
            approval_id=approval_id,
            version_id=version_id,
        )

    def staleness_transitioned(self, *, version_id: str) -> AuthoringTelemetry:
        return self._record(TelemetryEventKind.STALENESS_TRANSITIONED, version_id=version_id)
