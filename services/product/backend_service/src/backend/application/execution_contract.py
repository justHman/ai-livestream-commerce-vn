"""P0 execution evidence and command contract; no business or billing authority."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Literal, NamedTuple

from pydantic import BaseModel, Field

VERSION = "p0.execution.v1"
Phase = Literal["preparing", "ready", "warming", "selling", "closing", "ending", "ended", "failed"]
Kind = Literal["runtime_ready", "first_ai_broadcast", "health", "phase_changed", "terminal"]
Command = Literal["start", "hold", "resume", "interrupt", "end", "emergency_end"]

# Contract processing, the accepted comment envelope and approved speech are implemented.
# Real rescue, terminal reconciliation and signed usage
# remain unavailable until their owning tasks implement them.
AVAILABLE_CAPABILITIES = (
    "comment.p0.v1",
    "execution.evidence.v1",
    "execution.command_result.v1",
    "content.approved_speech.v1",
    "command.start",
)


def legacy_runtime_phase_hint(status: str) -> Phase | None:
    """Map only what old runtime status proves; active is not READY or live."""
    return {
        "starting": "preparing",
        "active": "preparing",
        "stopping": "ending",
        "failed": "failed",
    }.get(status)  # type: ignore[return-value]


class ExecutionIdentity(BaseModel):
    tenant_id: str = Field(min_length=1)
    business_session_id: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)


# C-TERMINAL-001 (NEW, proposed). Advertised only while the durable terminal
# store and outbox are configured AND enabled (terminal_outcomes.py); never part
# of the static tuple above, so the default stays "absent".
TERMINAL_CAPABILITY = "execution.terminal.v1"
_terminal_advertised = False


def set_terminal_advertised(value: bool) -> None:
    global _terminal_advertised
    _terminal_advertised = bool(value)


def available_capabilities() -> tuple[str, ...]:
    if _terminal_advertised:
        return (*AVAILABLE_CAPABILITIES, TERMINAL_CAPABILITY)
    return AVAILABLE_CAPABILITIES


class Capabilities(BaseModel):
    version: str = VERSION
    available: tuple[str, ...] = Field(default_factory=available_capabilities)

    def supports(self, *required: str) -> bool:
        return self.version == VERSION and set(required).issubset(self.available)


def start_command_id(identity: ExecutionIdentity) -> str:
    # Length-prefixed UTF-8 is shared with Go; delimiters inside IDs are safe.
    values = (
        identity.tenant_id,
        identity.business_session_id,
        identity.runtime_session_id,
        identity.generation,
    )
    data = b"".join(str(len(v.encode())).encode() + b":" + v.encode() for v in values)
    return "start:" + hashlib.sha256(data).hexdigest()


class MediaReadiness(BaseModel):
    readiness_id: str = Field(min_length=1)
    destination_id: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    media_ready: bool
    platform_ready: bool

    def ready(self) -> bool:
        return self.media_ready and self.platform_ready


class Evidence(ExecutionIdentity):
    sequence: int = Field(gt=0)
    kind: Kind
    phase: Phase
    reason_code: str | None = None
    occurred_at: datetime
    healthy: bool | None = None
    media_readiness: MediaReadiness | None = None
    opening_turn_id: str | None = None
    media_utterance_id: str | None = None
    media_evidence_id: str | None = None


class ExecutionState(ExecutionIdentity):
    sequence: int = 0
    phase: Phase = "preparing"
    runtime_ready: bool = False
    first_ai_broadcast: bool = False
    healthy: bool | None = None
    terminal_reason: str | None = None
    media_readiness: MediaReadiness | None = None
    approved_envelope_hash: str | None = None
    start_command_id: str | None = None
    opening_turn_id: str | None = None
    first_playable_evidence_id: str | None = None


class ContractRejection(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


_NEXT: dict[str, set[str]] = {
    "preparing": {"ready", "ending", "failed"},
    "ready": {"warming", "ending", "failed"},
    "warming": {"selling", "closing", "ending", "failed"},
    "selling": {"closing", "ending", "failed"},
    "closing": {"ending", "failed"},
    "ending": {"ended", "failed"},
}


def apply_evidence(state: ExecutionState, event: Evidence) -> ExecutionState:
    if state.model_dump(include=set(ExecutionIdentity.model_fields)) != event.model_dump(
        include=set(ExecutionIdentity.model_fields)
    ):
        raise ContractRejection("stale_generation")
    if event.sequence <= state.sequence:
        raise ContractRejection("stale_sequence")
    if state.phase in ("ended", "failed"):
        raise ContractRejection("already_terminal")
    changes: dict[str, object] = {}
    if event.kind == "runtime_ready":
        if state.phase != "preparing" or event.phase != "ready":
            raise ContractRejection("invalid_lifecycle_state")
        changes["runtime_ready"] = True
        changes["media_readiness"] = event.media_readiness
    elif event.kind == "first_ai_broadcast":
        if not state.runtime_ready or event.phase not in ("warming", "selling"):
            raise ContractRejection("runtime_not_ready")
        if (
            not state.start_command_id
            or not state.opening_turn_id
            or event.opening_turn_id != state.opening_turn_id
            or state.media_readiness is None
            or not state.media_readiness.ready()
            or event.media_readiness != state.media_readiness
            or not event.media_utterance_id
            or not event.media_evidence_id
        ):
            raise ContractRejection("uncorrelated_media")
        if state.first_ai_broadcast:
            raise ContractRejection("already_live")
        changes["first_ai_broadcast"] = True
        changes["first_playable_evidence_id"] = event.media_evidence_id
    elif event.kind == "health":
        if event.healthy is None or event.phase != state.phase:
            raise ContractRejection("invalid_lifecycle_state")
        changes["healthy"] = event.healthy
    elif event.kind in ("phase_changed", "terminal"):
        if event.phase in ("ready", "warming", "selling") and not state.runtime_ready:
            raise ContractRejection("runtime_not_ready")
    if event.phase != state.phase and event.phase not in _NEXT.get(state.phase, set()):
        raise ContractRejection("invalid_lifecycle_state")
    if event.phase == "failed" and event.reason_code != "execution_failed":
        raise ContractRejection("invalid_lifecycle_state")
    if event.phase == "ended" and event.reason_code not in ("normal_end", "merchant_emergency_end"):
        raise ContractRejection("invalid_lifecycle_state")
    if event.phase in ("ended", "failed"):
        changes["terminal_reason"] = event.reason_code
    return state.model_copy(update={**changes, "phase": event.phase, "sequence": event.sequence})


class CommandRequest(ExecutionIdentity):
    command_id: str = Field(min_length=1)
    command: str = Field(min_length=1)
    actor_id: str = Field(min_length=1)
    requested_at: datetime


class CommandOutcome(CommandRequest):
    status: Literal["accepted", "applied", "rejected"]
    reason_code: str | None = None
    result_at: datetime
    sequence: int | None = None
    opening_turn_id: str | None = None


def command_rejection(
    state: ExecutionState, request: CommandRequest, capabilities: Capabilities
) -> str | None:
    if state.model_dump(include=set(ExecutionIdentity.model_fields)) != request.model_dump(
        include=set(ExecutionIdentity.model_fields)
    ):
        return "stale_generation"
    if state.phase in ("ended", "failed"):
        return "already_terminal"
    if request.command not in ("start", "hold", "resume", "interrupt", "end", "emergency_end"):
        return "rejected_command"
    if not capabilities.supports(f"command.{request.command}"):
        return "unsupported_capability"
    return None


# ---------------------------------------------------------------------------
# C-TERMINAL-001: durable terminal outcome record, identity, canonical hash and
# the pure precedence reducer. Twin of pkg/executioncontract/terminal.go; both
# load terminal_record_v1.json byte-for-byte. Apply/apply_evidence above are
# deliberately unchanged: nothing here is wired to a producer.
# ---------------------------------------------------------------------------

TERMINAL_SCHEMA = "p0.terminal.v1"
_MAX_FIELD = 255
_MAX_REF = 512
_END_REASONS = ("normal_end", "merchant_emergency_end", "entitlement_exhausted")
_FAILURE_CLASSES = (
    "runtime_lost",
    "runtime_error",
    "platform_failed",
    "media_failed",
    "cleanup_failed",
    "safety_failed",
    "control_lost",
)
_CLEANUP_STATUSES = ("pending", "succeeded", "retrying", "failed")


class TerminalRecordInvalid(ValueError):
    pass


class CommandRef(BaseModel):
    command_id: str
    actor_id: str


class CleanupRefs(BaseModel):
    media: str | None = None
    livekit_room: str | None = None
    egress: str | None = None
    platform_live: str | None = None


class Cleanup(BaseModel):
    status: str = "pending"
    attempts: int = 0
    last_error_class: str | None = None
    refs: CleanupRefs = Field(default_factory=CleanupRefs)


class EvidenceRefs(BaseModel):
    last_execution_sequence: int | None = None
    usage_final_ref: str | None = None
    ledger_ref: str | None = None
    diagnostic_ref: str | None = None


class TerminalRecord(BaseModel):
    """Producer-owned terminal record. ``record_hash`` covers only the immutable core."""

    identity: ExecutionIdentity
    terminal_record_id: str = ""
    terminal_phase: str
    reason_code: str
    failure_class: str | None = None
    business_outcome: str
    source: str
    command_ref: CommandRef | None = None
    terminal_sequence: int = 0
    first_ai_broadcast: bool = False
    first_playable_evidence_id: str | None = None
    stop_requested_at: datetime | None = None
    closing_started_at: datetime | None = None
    ending_started_at: datetime | None = None
    terminal_at: datetime
    recorded_at: datetime
    cleanup: Cleanup = Field(default_factory=Cleanup)
    evidence_refs: EvidenceRefs = Field(default_factory=EvidenceRefs)
    record_hash: str = ""

    def sealed(self) -> "TerminalRecord":
        record = self.model_copy(update={"terminal_record_id": terminal_record_id(self.identity)})
        return record.model_copy(update={"record_hash": terminal_record_hash(record)})


def _length_prefixed(*values: str) -> str:
    return "".join(f"{len(v.encode())}:{v}" for v in values)


def terminal_record_id(identity: ExecutionIdentity) -> str:
    """``"tr:" + hex(SHA-256(L(tenant) L(session) L(runtime) L(generation)))``."""
    data = _length_prefixed(
        identity.tenant_id,
        identity.business_session_id,
        identity.runtime_session_id,
        identity.generation,
    )
    return "tr:" + hashlib.sha256(data.encode()).hexdigest()


def canonical_time(value: datetime | None) -> str:
    """UTC, microsecond precision (the storage precision), empty when null."""
    if value is None:
        return ""
    if value.tzinfo is None:
        raise TerminalRecordInvalid("timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"


def terminal_canonical_input(record: TerminalRecord) -> str:
    command = record.command_ref
    return _length_prefixed(
        TERMINAL_SCHEMA,
        record.identity.tenant_id,
        record.identity.business_session_id,
        record.identity.runtime_session_id,
        record.identity.generation,
        record.terminal_phase,
        record.reason_code,
        record.failure_class or "",
        record.business_outcome,
        record.source,
        command.command_id if command else "",
        command.actor_id if command else "",
        str(record.terminal_sequence),
        "true" if record.first_ai_broadcast else "false",
        record.first_playable_evidence_id or "",
        canonical_time(record.stop_requested_at),
        canonical_time(record.closing_started_at),
        canonical_time(record.ending_started_at),
        canonical_time(record.terminal_at),
        canonical_time(record.recorded_at),
    )


def terminal_record_hash(record: TerminalRecord) -> str:
    return hashlib.sha256(terminal_canonical_input(record).encode()).hexdigest()


def validate_terminal_record(record: TerminalRecord) -> None:
    """Shape, enums, length limits, the reason/phase/outcome matrix, derived id and hash."""

    def need(ok: bool, message: str) -> None:
        if not ok:
            raise TerminalRecordInvalid(message)

    ident = record.identity
    for name, value in ident.model_dump().items():
        need(bool(value.strip()), "incomplete identity")
        need(len(value.encode()) <= _MAX_FIELD, f"{name} exceeds {_MAX_FIELD} bytes")
    if record.terminal_phase == "ended":
        if record.reason_code in _END_REASONS:
            need(record.business_outcome == "ENDED", f"{record.reason_code} maps to ENDED")
        elif record.reason_code == "pre_live_cancel":
            need(record.business_outcome == "CANCELED", "pre_live_cancel maps to CANCELED")
            need(
                not record.first_ai_broadcast,
                "pre_live_cancel is not allowed after the first AI broadcast",
            )
        else:
            raise TerminalRecordInvalid("ended requires an end reason")
        need(record.failure_class is None, "failure_class is for failed only")
    elif record.terminal_phase == "failed":
        need(
            record.reason_code == "execution_failed" and record.business_outcome == "FAILED",
            "failed requires execution_failed and FAILED",
        )
        need(record.failure_class in _FAILURE_CLASSES, "failed requires a known failure_class")
    else:
        raise TerminalRecordInvalid("terminal_phase must be ended or failed")
    need(record.source in ("runtime", "api_supervisor", "merchant_command"), "unknown source")
    if record.source == "merchant_command":
        need(record.command_ref is not None, "merchant_command requires command_ref")
    if record.command_ref is not None:
        need(
            bool(record.command_ref.command_id.strip())
            and bool(record.command_ref.actor_id.strip()),
            "command_ref needs command_id and actor_id",
        )
        need(len(record.command_ref.command_id.encode()) <= _MAX_FIELD, "command_id too long")
        need(len(record.command_ref.actor_id.encode()) <= _MAX_FIELD, "actor_id too long")
    need(record.terminal_sequence >= 0, "terminal_sequence must not be negative")
    need(
        record.first_playable_evidence_id is None
        or len(record.first_playable_evidence_id.encode()) <= _MAX_FIELD,
        "first_playable_evidence_id too long",
    )
    for stamp in (
        record.stop_requested_at,
        record.closing_started_at,
        record.ending_started_at,
        record.terminal_at,
        record.recorded_at,
    ):
        canonical_time(stamp)  # rejects naive datetimes
    cleanup = record.cleanup
    need(cleanup.status in _CLEANUP_STATUSES, "unknown cleanup status")
    need(0 <= cleanup.attempts <= 1_000_000, "cleanup attempts out of range")
    need(
        cleanup.last_error_class is None or len(cleanup.last_error_class.encode()) <= 64,
        "cleanup.last_error_class too long",
    )
    refs = [
        *cleanup.refs.model_dump().values(),
        record.evidence_refs.usage_final_ref,
        record.evidence_refs.ledger_ref,
        record.evidence_refs.diagnostic_ref,
    ]
    need(all(v is None or len(v.encode()) <= _MAX_REF for v in refs), "ref exceeds 512 bytes")
    need(
        record.terminal_record_id == terminal_record_id(ident),
        "terminal_record_id does not match identity",
    )
    need(
        record.record_hash == terminal_record_hash(record),
        "record_hash does not match the canonical record",
    )


class TerminalDecision(NamedTuple):
    action: str  # create | replay | reject
    reason: str = ""
    audit: str = ""  # conflicting_terminal | late_success | late_failure
    cleanup_failure: bool = False


def reduce_terminal(existing: TerminalRecord | None, incoming: TerminalRecord) -> TerminalDecision:
    """C-TERMINAL-001 precedence for one (tenant, business session, generation).

    The first durable terminal wins; FAILED is never rewritten to ENDED; a late
    success cannot erase a failure; a failure after ENDED is cleanup evidence only.
    """
    if existing is None:
        return TerminalDecision("create")
    if (
        existing.terminal_record_id == incoming.terminal_record_id
        and existing.record_hash == incoming.record_hash
    ):
        return TerminalDecision("replay")
    audit, cleanup_failure = "conflicting_terminal", False
    if existing.terminal_record_id == incoming.terminal_record_id:
        if existing.terminal_phase == "failed" and incoming.terminal_phase == "ended":
            audit = "late_success"
        elif existing.terminal_phase == "ended" and incoming.terminal_phase == "failed":
            audit, cleanup_failure = "late_failure", True
    return TerminalDecision("reject", "already_terminal", audit, cleanup_failure)


def _fill(existing: str | None, incoming: str | None) -> str | None:
    return existing if existing is not None else incoming


def reduce_cleanup(existing: Cleanup, incoming: Cleanup) -> tuple[Cleanup, bool]:
    """Monotonic merge: a final status is never reopened, attempts never decrease, refs only fill."""
    if existing.status in ("succeeded", "failed"):
        return existing, False
    rank = {"pending": 0, "retrying": 1}
    newer = incoming.attempts > existing.attempts or (
        incoming.attempts == existing.attempts
        and rank.get(incoming.status, 2) > rank.get(existing.status, 2)
    )
    if not newer:
        return existing, False
    refs = CleanupRefs(
        media=_fill(existing.refs.media, incoming.refs.media),
        livekit_room=_fill(existing.refs.livekit_room, incoming.refs.livekit_room),
        egress=_fill(existing.refs.egress, incoming.refs.egress),
        platform_live=_fill(existing.refs.platform_live, incoming.refs.platform_live),
    )
    return incoming.model_copy(update={"refs": refs}), True
