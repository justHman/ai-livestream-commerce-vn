from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from backend.api.v1.execution import record_execution_evidence, request_execution_command
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import (
    Capabilities,
    CommandRequest,
    ContractRejection,
    Evidence,
    ExecutionState,
    apply_evidence,
    command_rejection,
    legacy_runtime_phase_hint,
    MediaReadiness,
)


IDENTITY = dict(
    tenant_id="tenant",
    business_session_id="business",
    runtime_session_id="runtime",
    generation="g1",
)
NOW = datetime.now(timezone.utc)


def evidence(sequence, kind, phase, **changes):
    return Evidence(
        **(IDENTITY | changes), sequence=sequence, kind=kind, phase=phase, occurred_at=NOW
    )


def test_readiness_ordering_generation_and_terminal_precedence():
    state = ExecutionState(**IDENTITY)
    with pytest.raises(ContractRejection, match="runtime_not_ready"):
        apply_evidence(state, evidence(1, "first_ai_broadcast", "warming"))
    state = apply_evidence(state, evidence(1, "runtime_ready", "ready"))
    assert state.runtime_ready and not state.first_ai_broadcast
    with pytest.raises(ContractRejection, match="stale_sequence"):
        apply_evidence(state, evidence(1, "runtime_ready", "ready"))
    with pytest.raises(ContractRejection, match="stale_generation"):
        apply_evidence(state, evidence(2, "health", "ready", generation="old", healthy=True))
    media = MediaReadiness(
        readiness_id="r", destination_id="d", source_id="s", media_ready=True, platform_ready=True
    )
    state = state.model_copy(
        update={"start_command_id": "start", "opening_turn_id": "opening", "media_readiness": media}
    )
    state = apply_evidence(
        state,
        evidence(
            2,
            "first_ai_broadcast",
            "warming",
            media_readiness=media,
            opening_turn_id="opening",
            media_utterance_id="u",
            media_evidence_id="e",
        ),
    )
    assert state.first_ai_broadcast
    with pytest.raises(ContractRejection, match="invalid_lifecycle_state"):
        apply_evidence(state, evidence(3, "phase_changed", "ready"))
    state = apply_evidence(state, evidence(3, "phase_changed", "ending"))
    state = apply_evidence(state, evidence(4, "terminal", "failed", reason_code="execution_failed"))
    with pytest.raises(ContractRejection, match="already_terminal"):
        apply_evidence(state, evidence(5, "terminal", "ended", reason_code="normal_end"))
    for reason in ("normal_end", "merchant_emergency_end"):
        ended = apply_evidence(
            ExecutionState(**IDENTITY, phase="ending", runtime_ready=True),
            evidence(1, "terminal", "ended", reason_code=reason),
        )
        assert ended.terminal_reason == reason
    with pytest.raises(ContractRejection, match="stale_generation"):
        apply_evidence(
            ExecutionState(**(IDENTITY | {"generation": "g2"})),
            evidence(1, "runtime_ready", "ready"),
        )


def test_truthful_command_capabilities():
    assert legacy_runtime_phase_hint("active") == "preparing"
    assert legacy_runtime_phase_hint("stopped") is None
    state = ExecutionState(**IDENTITY, phase="ready", runtime_ready=True)
    req = CommandRequest(
        **IDENTITY, command_id="c1", command="hold", actor_id="operator", requested_at=NOW
    )
    assert command_rejection(state, req, Capabilities()) == "unsupported_capability"
    assert not Capabilities().supports("command.hold", "autonomous_start", "signed_usage")
    assert (
        command_rejection(state, req.model_copy(update={"command": "unknown"}), Capabilities())
        == "rejected_command"
    )
    assert (
        command_rejection(state, req.model_copy(update={"generation": "old"}), Capabilities())
        == "stale_generation"
    )
    assert (
        command_rejection(state.model_copy(update={"phase": "ended"}), req, Capabilities())
        == "already_terminal"
    )


@pytest.mark.asyncio
async def test_command_endpoint_duplicate_and_evidence_fence():
    store = InMemorySessionStore()
    await store.set(
        "runtime",
        {
            "execution_contract": ExecutionState(**IDENTITY).model_dump(mode="json"),
            "execution_command_outcomes": {},
        },
    )
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(container=SimpleNamespace(store=store))),
        }
    )
    req = CommandRequest(
        **IDENTITY, command_id="c1", command="interrupt", actor_id="operator", requested_at=NOW
    )
    first = await request_execution_command("runtime", req, request, None)
    assert first["outcome"]["status"] == "rejected"
    assert first["outcome"]["reason_code"] == "unsupported_capability"
    duplicate = await request_execution_command("runtime", req, request, None)
    assert duplicate["replayed"] and duplicate["outcome"] == first["outcome"]
    with pytest.raises(HTTPException) as conflict:
        await request_execution_command(
            "runtime", req.model_copy(update={"command": "end"}), request, None
        )
    assert conflict.value.status_code == 409
    ready = await record_execution_evidence(
        "runtime", evidence(1, "runtime_ready", "ready"), request, None
    )
    assert ready["state"]["phase"] == "ready"
    with pytest.raises(HTTPException) as stale:
        await record_execution_evidence(
            "runtime", evidence(1, "runtime_ready", "ready"), request, None
        )
    assert stale.value.detail["code"] == "stale_sequence"
