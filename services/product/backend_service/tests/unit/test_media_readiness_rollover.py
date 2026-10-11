from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

from backend.api.v1.execution import record_execution_evidence
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import (
    ContractRejection,
    Evidence,
    ExecutionState,
    MediaReadiness,
    apply_evidence,
)


IDENTITY = dict(
    tenant_id="tenant",
    business_session_id="business",
    runtime_session_id="runtime",
    generation="g1",
)
OLD = MediaReadiness(
    readiness_id="mr:1",
    destination_id="dest-1",
    source_id="lk:runtime/avatar/TR_v+TR_a",
    media_ready=True,
    platform_ready=True,
)
NEW = OLD.model_copy(update={"source_id": "lk:runtime/avatar/TR_v2+TR_a2"})


def ready_state(**changes):
    return ExecutionState(
        **IDENTITY,
        **{
            "phase": "ready",
            "sequence": 4,
            "runtime_ready": True,
            "media_readiness": OLD,
            "approved_envelope_hash": "approved",
            **changes,
        },
    )


def renewal(media=NEW, **changes):
    return Evidence(
        **IDENTITY,
        **{
            "sequence": 5,
            "kind": "runtime_ready",
            "phase": "ready",
            "occurred_at": datetime.now(timezone.utc),
            "media_readiness": media,
            **changes,
        },
    )


def test_rollover_renews_track_source_without_changing_execution_or_starting_broadcast():
    updated = apply_evidence(ready_state(), renewal())
    assert updated.media_readiness == NEW
    assert updated.sequence == 5 and updated.phase == "ready"
    assert updated.approved_envelope_hash == "approved"
    assert updated.runtime_ready and not updated.first_ai_broadcast
    assert not updated.start_command_id and not updated.opening_turn_id


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "warming"},
        {"phase": "selling"},
        {"phase": "closing"},
        {"phase": "ending"},
        {"runtime_ready": False},
        {"start_command_id": "start"},
        {"opening_turn_id": "opening"},
        {"first_ai_broadcast": True},
        {"first_playable_evidence_id": "me:1"},
        {"media_readiness": None},
    ],
)
def test_renewal_cannot_cross_start_or_missing_readiness(changes):
    with pytest.raises(ContractRejection, match="invalid_lifecycle_state"):
        apply_evidence(ready_state(**changes), renewal())


@pytest.mark.parametrize(
    "changes",
    [
        {"readiness_id": "mr:other"},
        {"destination_id": "dest-other"},
        {"media_ready": False},
        {"platform_ready": False},
        {"source_id": "lk:other/avatar/TR_v2+TR_a2"},
        {"source_id": "lk:runtime/other/TR_v2+TR_a2"},
        {"source_id": "lk:runtime/avatar/"},
        {"source_id": "lk:runtime/avatar/TR_v2+"},
        {"source_id": "lk:runtime/avatar/TR_v2"},
        {"source_id": "unrelated"},
    ],
)
def test_renewal_is_only_a_track_change_for_the_same_ready_source(changes):
    with pytest.raises(ContractRejection, match="invalid_lifecycle_state"):
        apply_evidence(ready_state(), renewal(NEW.model_copy(update=changes)))


@pytest.mark.asyncio
async def test_evidence_endpoint_renews_source_and_preserves_approved_envelope():
    store = InMemorySessionStore()
    await store.set("runtime", {"execution_contract": ready_state().model_dump(mode="json")})

    async def resolve(_session_id):
        return SimpleNamespace(fingerprint="approved")

    container = SimpleNamespace(
        store=store,
        coordinator=SimpleNamespace(has=lambda _: True),
        approved_speech=SimpleNamespace(resolve=resolve),
    )
    request = Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(container=container))}
    )
    result = await record_execution_evidence("runtime", renewal(), request, None)
    assert result["state"]["media_readiness"]["source_id"] == NEW.source_id
    assert result["state"]["approved_envelope_hash"] == "approved"

    async def changed_envelope(_session_id):
        return SimpleNamespace(fingerprint="changed")

    container.approved_speech.resolve = changed_envelope
    with pytest.raises(HTTPException) as rejected:
        await record_execution_evidence("runtime", renewal(OLD, sequence=6), request, None)
    assert rejected.value.status_code == 409
    persisted = await store.get("runtime")
    assert persisted["execution_contract"]["sequence"] == 5
    assert persisted["execution_contract"]["approved_envelope_hash"] == "approved"


def test_first_broadcast_must_use_the_renewed_source():
    updated = apply_evidence(ready_state(), renewal())
    started = updated.model_copy(update={"start_command_id": "start", "opening_turn_id": "opening"})
    first = renewal(
        NEW,
        sequence=6,
        kind="first_ai_broadcast",
        phase="warming",
        opening_turn_id="opening",
        media_utterance_id="utterance",
        media_evidence_id="me:1",
    )
    with pytest.raises(ContractRejection, match="uncorrelated_media"):
        apply_evidence(started, first.model_copy(update={"media_readiness": OLD}))
    assert apply_evidence(started, first).first_ai_broadcast
