"""P0 execution evidence and command contract; no business or billing authority."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

VERSION = "p0.execution.v1"
Phase = Literal["preparing", "ready", "warming", "selling", "closing", "ending", "ended", "failed"]
Kind = Literal["runtime_ready", "first_ai_broadcast", "health", "phase_changed", "terminal"]
Command = Literal["hold", "resume", "interrupt", "end", "emergency_end"]

# Only contract processing and the accepted comment envelope are implemented.
# Real rescue, autonomous opening, terminal reconciliation and signed usage
# remain unavailable until their owning tasks implement them.
AVAILABLE_CAPABILITIES = ("comment.p0.v1", "execution.evidence.v1", "execution.command_result.v1")


def legacy_runtime_phase_hint(status: str) -> Phase | None:
    """Map only what old runtime status proves; active is not READY or live."""
    return {"starting": "preparing", "active": "preparing", "stopping": "ending", "failed": "failed"}.get(status)  # type: ignore[return-value]


class ExecutionIdentity(BaseModel):
    tenant_id: str = Field(min_length=1)
    business_session_id: str = Field(min_length=1)
    runtime_session_id: str = Field(min_length=1)
    generation: str = Field(min_length=1)


class Capabilities(BaseModel):
    version: str = VERSION
    available: tuple[str, ...] = AVAILABLE_CAPABILITIES

    def supports(self, *required: str) -> bool:
        return self.version == VERSION and set(required).issubset(self.available)


class Evidence(ExecutionIdentity):
    sequence: int = Field(gt=0)
    kind: Kind
    phase: Phase
    reason_code: str | None = None
    occurred_at: datetime
    healthy: bool | None = None


class ExecutionState(ExecutionIdentity):
    sequence: int = 0
    phase: Phase = "preparing"
    runtime_ready: bool = False
    first_ai_broadcast: bool = False
    healthy: bool | None = None
    terminal_reason: str | None = None


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
    elif event.kind == "first_ai_broadcast":
        if not state.runtime_ready or event.phase not in ("warming", "selling"):
            raise ContractRejection("runtime_not_ready")
        changes["first_ai_broadcast"] = True
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


def command_rejection(state: ExecutionState, request: CommandRequest, capabilities: Capabilities) -> str | None:
    if state.model_dump(include=set(ExecutionIdentity.model_fields)) != request.model_dump(
        include=set(ExecutionIdentity.model_fields)
    ):
        return "stale_generation"
    if state.phase in ("ended", "failed"):
        return "already_terminal"
    if request.command not in ("hold", "resume", "interrupt", "end", "emergency_end"):
        return "rejected_command"
    if not capabilities.supports(f"command.{request.command}"):
        return "unsupported_capability"
    return None
