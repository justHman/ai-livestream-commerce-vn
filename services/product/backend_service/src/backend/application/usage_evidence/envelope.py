"""C-USAGE-EVIDENCE-001 envelope: ids, derivation from applied facts, body bytes.

Pure and deterministic. No I/O, no clock, no billing arithmetic: the Runtime
reports what it applied; the Livento backend alone computes Live Credits.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from backend.application.execution_contract import (
    VERSION,
    CommandOutcome,
    Evidence,
    ExecutionIdentity,
    ExecutionState,
    start_command_id,
)

EVENT_TYPE = "ai.execution.usage_evidence"
COGS_EVENT_TYPE = "ai.usage.reported"
SCHEMA = "p0.usage_evidence.v1"
CAP = "usage.evidence.v1"
CAP_FIRST_BROADCAST = "usage.evidence.first_broadcast"
CAP_MEDIA_HEALTH = "usage.evidence.media_health"
CAP_HOLD = "usage.evidence.hold"
MAX_ID = 255  # webhook_events.event_id and identity fields (migration 000012)
MAX_BODY = 512 * 1024  # receiver limit is 1 MiB; stay well below it
_UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
_PHASE_INTERVALS = ("warming", "selling", "closing", "ending")
# ponytail: provisional until C-MEDIA-001/010 freezes a media-health field on Evidence.
_MEDIA_REASON_PREFIXES = ("media_", "platform_")


class InvalidIdentity(ValueError):
    """The identity can never be delivered (non-UUID tenant, oversize id)."""


@dataclass(frozen=True)
class Gates:
    """Capability gates for kinds whose dependency is not frozen. All off by default."""

    first_broadcast: bool = False
    media_health: bool = False
    hold: bool = False


@dataclass(frozen=True)
class Draft:
    """One fact to report. ``opening_ref=None`` means: resolve from the stored start."""

    kind: str
    interval_kind: str
    boundary: str
    opening_ref: str | None
    phase: str
    occurred_at: datetime
    applied_sequence: int
    execution_sequence: int | None = None
    reason_code: str | None = None
    command_id: str | None = None
    health_source: str = "runtime"
    media: Mapping[str, Any] | None = None


def length_prefixed(*values: str) -> bytes:
    return b"".join(str(len(v.encode())).encode() + b":" + v.encode() for v in values)


def _identity_values(identity: ExecutionIdentity) -> tuple[str, ...]:
    return (
        identity.tenant_id,
        identity.business_session_id,
        identity.runtime_session_id,
        identity.generation,
    )


def interval_id(identity: ExecutionIdentity, interval_kind: str, opening_ref: str) -> str:
    data = length_prefixed(*_identity_values(identity), interval_kind, opening_ref)
    return "ui:" + hashlib.sha256(data).hexdigest()


def event_id(identity: ExecutionIdentity, kind: str, interval: str) -> str:
    data = length_prefixed(*_identity_values(identity), kind, interval)
    return "ue:" + hashlib.sha256(data).hexdigest()


def validate_identity(identity: ExecutionIdentity) -> None:
    if not _UUID.match(identity.tenant_id):
        raise InvalidIdentity("tenant_id is not a UUID")
    for value in _identity_values(identity):
        if len(value) > MAX_ID:
            raise InvalidIdentity("identity field too long")


def identity_of(state: ExecutionState) -> ExecutionIdentity:
    return ExecutionIdentity(**state.model_dump(include=set(ExecutionIdentity.model_fields)))


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(document: Mapping[str, Any]) -> bytes:
    """The single serialization. Serialized once at insert, resent byte-for-byte."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def build_body(
    identity: ExecutionIdentity,
    draft: Draft,
    *,
    interval: str,
    usage_sequence: int,
    producer_id: str,
) -> tuple[str, bytes]:
    """Return ``(event_id, body bytes)`` for a lifecycle row."""
    eid = event_id(identity, draft.kind, interval)
    occurred = utc_text(draft.occurred_at)
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "execution_contract_version": VERSION,
        "identity": identity.model_dump(),
        "usage_sequence": usage_sequence,
        "execution_sequence": draft.execution_sequence,
        "kind": draft.kind,
        "phase": draft.phase,
        "occurred_at": occurred,
        "interval": {
            "interval_id": interval,
            "interval_kind": draft.interval_kind,
            "boundary": draft.boundary,
        },
        "reason_code": draft.reason_code,
        "health_source": draft.health_source,
        "producer_id": producer_id,
    }
    if draft.command_id is not None:
        payload["command_id"] = draft.command_id
    if draft.media is not None:
        payload["media"] = dict(draft.media)
    body = canonical(
        {
            "event_id": eid,
            "event_type": EVENT_TYPE,
            "event_version": "1.0",
            "tenant_id": identity.tenant_id,
            "stream_id": identity.business_session_id,
            "ai_session_id": identity.runtime_session_id,
            "request_id": start_command_id(identity),
            "timestamp": occurred,
            "payload": payload,
        }
    )
    if len(body) > MAX_BODY:
        raise InvalidIdentity("body too large")
    return eid, body


def draft_to_dict(draft: Draft) -> dict[str, Any]:
    """JSON-safe form of a Draft (deferred usage evidence kept in the session meta)."""
    return {
        "kind": draft.kind,
        "interval_kind": draft.interval_kind,
        "boundary": draft.boundary,
        "opening_ref": draft.opening_ref,
        "phase": draft.phase,
        "occurred_at": utc_text(draft.occurred_at),
        "applied_sequence": draft.applied_sequence,
        "execution_sequence": draft.execution_sequence,
        "reason_code": draft.reason_code,
        "command_id": draft.command_id,
        "health_source": draft.health_source,
        "media": dict(draft.media) if draft.media is not None else None,
    }


def draft_from_dict(raw: Mapping[str, Any]) -> Draft:
    fields = dict(raw)
    fields["occurred_at"] = datetime.fromisoformat(
        str(fields["occurred_at"]).replace("Z", "+00:00")
    )
    return Draft(**fields)


def finalize_body(staged_body: bytes, usage_sequence: int) -> bytes:
    """Stamp the final ``usage_sequence`` into a staged body (ready-time numbering).

    ``canonical`` is deterministic, so this equals ``build_body(..., usage_sequence=n)``.
    The result is stored once and then resent byte-for-byte.
    """
    document = json.loads(staged_body)
    document["payload"]["usage_sequence"] = usage_sequence
    return canonical(document)


def build_cogs_body(
    identity: ExecutionIdentity,
    *,
    sample_id: str,
    occurred_at: datetime,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    producer_id: str,
) -> tuple[str, bytes]:
    """Vendor COGS on the existing ``ai.usage.reported``: no interval, never billing."""
    eid = (
        "uc:" + hashlib.sha256(length_prefixed(*_identity_values(identity), sample_id)).hexdigest()
    )
    ts = utc_text(occurred_at)
    return eid, canonical(
        {
            "event_id": eid,
            "event_type": COGS_EVENT_TYPE,
            "event_version": "1.0",
            "tenant_id": identity.tenant_id,
            "stream_id": identity.business_session_id,
            "ai_session_id": identity.runtime_session_id,
            "request_id": start_command_id(identity),
            "timestamp": ts,
            "payload": {
                "job_id": sample_id,
                "model_id": model_id,
                "input_tokens": max(0, int(input_tokens)),
                "output_tokens": max(0, int(output_tokens)),
                "producer_id": producer_id,
            },
        }
    )


def derive_from_evidence(
    prior: ExecutionState, updated: ExecutionState, ev: Evidence, gates: Gates
) -> list[Draft]:
    """Rows for evidence that ``apply_evidence`` already accepted. Never for rejected."""
    base: dict[str, Any] = dict(
        phase=updated.phase,
        occurred_at=ev.occurred_at,
        applied_sequence=updated.sequence,
        execution_sequence=ev.sequence,
    )
    out: list[Draft] = []
    if ev.kind == "first_ai_broadcast" and gates.first_broadcast:
        readiness = ev.media_readiness
        out.append(
            Draft(
                "first_ai_broadcast",
                "ai_live",
                "start",
                "",
                **base,
                media={
                    "opening_turn_id": ev.opening_turn_id,
                    "media_utterance_id": ev.media_utterance_id,
                    "media_evidence_id": ev.media_evidence_id,
                    "readiness_id": readiness.readiness_id if readiness else None,
                },
            )
        )
    elif ev.kind == "health" and ev.healthy is not None:
        media = str(ev.reason_code or "").startswith(_MEDIA_REASON_PREFIXES)
        if not media or gates.media_health:
            source = "media" if media else "runtime"
            if ev.healthy is False and prior.healthy is not False:
                out.append(
                    Draft(
                        "unusable_started",
                        "unusable",
                        "start",
                        str(ev.sequence),
                        **base,
                        reason_code=ev.reason_code,
                        health_source=source,
                    )
                )
            elif ev.healthy is True and prior.healthy is False:
                out.append(
                    Draft(
                        "unusable_ended",
                        "unusable",
                        "end",
                        None,
                        **base,
                        reason_code=ev.reason_code,
                        health_source=source,
                    )
                )
    elif (
        ev.kind == "phase_changed" and ev.phase in _PHASE_INTERVALS and updated.phase != prior.phase
    ):
        out.append(Draft("phase_changed", f"phase:{ev.phase}", "start", "", **base))
    if updated.phase in ("ended", "failed") and prior.phase not in ("ended", "failed"):
        out.append(Draft("terminal", "terminal", "point", "", **base, reason_code=ev.reason_code))
    return out


def derive_from_command(
    prior: ExecutionState, updated: ExecutionState, outcome: CommandOutcome, gates: Gates
) -> list[Draft]:
    """Rows for an APPLIED rescue outcome only. Rejected or accepted-only emit nothing."""
    if outcome.status != "applied":
        return []
    base: dict[str, Any] = dict(
        phase=updated.phase, occurred_at=outcome.result_at, applied_sequence=updated.sequence
    )
    out: list[Draft] = []
    if outcome.command == "hold" and gates.hold:
        out.append(
            Draft(
                "hold_started",
                "hold",
                "start",
                outcome.command_id,
                **base,
                command_id=outcome.command_id,
            )
        )
    elif (
        outcome.command in ("resume", "end", "emergency_end")
        and gates.hold
        and prior.hold.held
        and not updated.hold.held
        and prior.hold.hold_command_id
    ):
        out.append(
            Draft(
                "hold_ended",
                "hold",
                "end",
                prior.hold.hold_command_id,
                **base,
                command_id=prior.hold.hold_command_id,
            )
        )
    if outcome.command == "end" and prior.phase != "closing":
        out.append(Draft("phase_changed", "phase:closing", "start", "", **base))
    elif outcome.command == "emergency_end" and prior.phase != "ending":
        out.append(Draft("phase_changed", "phase:ending", "start", "", **base))
    return out
