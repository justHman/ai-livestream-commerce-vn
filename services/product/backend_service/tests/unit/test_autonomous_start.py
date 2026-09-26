"""007 uses real HTTP/Coordinator/speech dispatch with controlled media only."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from backend.application.execution_contract import ExecutionIdentity, start_command_id
from backend.application.script_authoring.runtime_handoff import ResolvedApprovedScript
from . import test_approved_speech_active as speech_tests
from backend.api.v1 import execution

case_factory = speech_tests.case_factory
MEDIA = dict(
    readiness_id="fixture-ready-1",
    destination_id="fixture-destination",
    source_id="fixture-source",
    media_ready=True,
    platform_ready=True,
)


def identity(case):
    return dict(
        tenant_id="tenant-1",
        business_session_id="business-1",
        runtime_session_id=case.sid,
        generation="generation-1",
    )


def command(case, **changes):
    return (
        dict(
            **identity(case),
            command="start",
            command_id=start_command_id(ExecutionIdentity(**identity(case))),
            actor_id="owner-1",
            requested_at="2026-09-26T00:00:00Z",
        )
        | changes
    )


async def request(case, body=None):
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/execution/commands", json=body or command(case)
    )
    assert response.status_code == 200, response.text
    return response.json()


async def state(case):
    return (await case.client.get(f"/api/v1/sessions/{case.sid}/execution")).json()["state"]


async def ready(case, media=MEDIA):
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/execution/evidence",
        json=dict(
            **identity(case),
            kind="runtime_ready",
            phase="ready",
            sequence=1,
            occurred_at=datetime.now(timezone.utc).isoformat(),
            media_readiness=media,
        ),
    )
    assert response.status_code == 200, response.text
    return response.json()["state"]


async def prepared(factory, *, mark_ready=True, cloud=False):
    case = await factory(bind=False, cloud=cloud)
    response = await case.client.put(
        f"/api/v1/sessions/{case.sid}/script-set",
        json=dict(
            script_set_id=case.source.set.id, tenant_id="tenant-1", business_session_id="business-1"
        ),
    )
    assert response.status_code == 200, response.text
    response = await case.client.post(f"/api/v1/sessions/{case.sid}/attach", json={"products": []})
    assert response.status_code == 200, response.text
    if mark_ready:
        await ready(case)
    return case


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def no_output(case):
    assert not case.llm.started.is_set()
    assert not case.tts.calls
    assert not getattr(case.backend, "calls", [])
    assert not case.d.coordinator.opening_media(case.sid)
    assert not any(e["type"] == "coordinator.speak_started" for e in case.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [False, True])
async def test_no_comment_start_and_concurrent_semantic_retries(case_factory, cloud):
    case = await prepared(case_factory, cloud=cloud)
    before = await state(case)
    no_output(case)
    assert before["runtime_ready"] and not before["first_ai_broadcast"]
    results = await asyncio.gather(
        *(request(case, command(case, actor_id=f"owner-{n}")) for n in range(12))
    )
    assert sum(not r["replayed"] for r in results) == 1
    assert all(r["outcome"] == results[0]["outcome"] for r in results)
    assert results[0]["outcome"]["status"] == "applied"
    assert results[0]["outcome"]["reason_code"] == "opening_scheduled"
    await until(lambda: case.d.coordinator.opening_media(case.sid))
    await until(lambda: any(e["type"] == "coordinator.speak_finished" for e in case.events))
    assert (
        sum(
            e["type"] == "coordinator.speak_started" and e.get("action") == "autonomous_opening"
            for e in case.events
        )
        == 1
    )
    assert not case.llm.started.is_set()
    assert not (await state(case))["first_ai_broadcast"]
    again = await request(case)
    assert again["replayed"] and again["outcome"] == results[0]["outcome"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["preparing", "ready"])
async def test_hold_before_start_is_truthful_without_side_effect(case_factory, phase):
    case = await prepared(case_factory, mark_ready=phase == "ready")
    before = await state(case)
    hold = command(case, command="hold", command_id="hold-1")
    first, retry = await request(case, hold), await request(case, hold)
    assert first["outcome"]["status"] == "rejected"
    assert first["outcome"]["reason_code"] == "unsupported_capability"
    assert retry["replayed"] and retry["outcome"] == first["outcome"]
    assert await state(case) == before
    await case.d.coordinator._tick_once(case.sid)
    no_output(case)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["tenant_id", "business_session_id", "runtime_session_id", "generation", "command_id"]
)
async def test_wrong_identity_cannot_start_or_poison_valid_start(case_factory, field):
    case = await prepared(case_factory)
    rejected = await request(case, command(case, **{field: "wrong"}))
    assert rejected["outcome"]["status"] == "rejected"
    no_output(case)
    assert (await request(case))["outcome"]["status"] == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing",
    [
        "runtime_ready",
        "media",
        "media_ready",
        "platform_ready",
        "binding",
        "stale",
        "wrong_tenant",
        "detached",
    ],
)
async def test_missing_readiness_and_binding_fail_closed(case_factory, missing):
    case = await prepared(case_factory, mark_ready=False)
    if missing != "runtime_ready":
        media = (
            None
            if missing == "media"
            else MEDIA | ({missing: False} if missing in ("media_ready", "platform_ready") else {})
        )
        await ready(case, media)
    if missing == "binding":
        meta = await case.d.store.get(case.sid)
        meta.pop("script_set_binding")
        await case.d.store.set(case.sid, meta)
    elif missing == "stale":
        case.source.item.approved_version_id = "stale"
    elif missing == "wrong_tenant":
        case.source.set.brief.tenant_id = "another-tenant"
    elif missing == "detached":
        case.d.coordinator.stop(case.sid)
    result = await request(case)
    assert result["outcome"]["status"] == "rejected", result
    no_output(case)
    assert not (await state(case))["first_ai_broadcast"]


@pytest.mark.asyncio
async def test_readiness_rejection_does_not_consume_start_identity(case_factory):
    case = await prepared(case_factory, mark_ready=False)
    assert (await request(case))["outcome"]["reason_code"] == "runtime_not_ready"
    await ready(case)
    assert (await request(case))["outcome"]["status"] == "applied"


@pytest.mark.asyncio
async def test_delayed_correlated_media_is_only_first_playable_transition(case_factory):
    case = await prepared(case_factory)
    await request(case)
    before = await state(case)
    assert not before["first_ai_broadcast"] and before["phase"] == "ready"
    await until(lambda: case.d.coordinator.opening_media(case.sid))
    receipt = case.d.coordinator.opening_media(case.sid)
    event = dict(
        **identity(case),
        kind="first_ai_broadcast",
        phase="warming",
        sequence=2,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        media_readiness=MEDIA,
        opening_turn_id=receipt["opening_turn_id"],
        media_utterance_id=receipt["media_utterance_id"],
        media_evidence_id="playback-fixture-1",
    )
    url = f"/api/v1/sessions/{case.sid}/execution/evidence"
    for wrong in [
        {"opening_turn_id": "other"},
        {"media_utterance_id": "other"},
        {"generation": "old"},
        {"media_readiness": MEDIA | {"destination_id": "other"}},
        {"media_readiness": MEDIA | {"source_id": "other"}},
    ]:
        response = await case.client.post(url, json=event | wrong)
        assert response.status_code == 409
        assert not (await state(case))["first_ai_broadcast"]
    first = await case.client.post(url, json=event)
    assert first.status_code == 200, first.text
    after = first.json()["state"]
    assert (
        after["first_ai_broadcast"] and after["first_playable_evidence_id"] == "playback-fixture-1"
    )
    for duplicate in [event, event | {"sequence": 3, "media_evidence_id": "same-media-new-event"}]:
        assert (await case.client.post(url, json=duplicate)).status_code == 409
        assert await state(case) == after


@pytest.mark.asyncio
async def test_attach_room_join_and_scheduling_are_not_media_evidence(case_factory, monkeypatch):
    case = await prepared(case_factory)
    entered, release = asyncio.Event(), asyncio.Event()
    original = case.d.approved_speech.prepare

    async def delayed(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(case.d.approved_speech, "prepare", delayed)
    await case.d.hub.emit(case.sid, {"type": "room.joined"})
    before = await state(case)
    assert not before["first_ai_broadcast"]
    await request(case)
    await asyncio.wait_for(entered.wait(), 2)
    no_output(case)
    assert not (await state(case))["first_ai_broadcast"]
    # A forged playback claim cannot pass just because the opening was scheduled.
    event = dict(
        **identity(case),
        kind="first_ai_broadcast",
        phase="warming",
        sequence=2,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        media_readiness=MEDIA,
        opening_turn_id=command(case)["command_id"] + ":opening",
        media_utterance_id="fabricated",
        media_evidence_id="fake",
    )
    assert (
        await case.client.post(f"/api/v1/sessions/{case.sid}/execution/evidence", json=event)
    ).status_code == 409
    release.set()


@pytest.mark.asyncio
async def test_existing_stop_fences_late_opening_preparation(case_factory, monkeypatch):
    case = await prepared(case_factory)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = case.d.approved_speech.prepare

    async def late(*args, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()  # Emulate a provider that returns after cancel.
        try:
            return await original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(case.d.approved_speech, "prepare", late)
    await request(case)
    await asyncio.wait_for(entered.wait(), 2)
    stopped = await case.client.post(f"/api/v1/sessions/{case.sid}/stop")
    assert stopped.status_code == 200, stopped.text
    release.set()
    await asyncio.wait_for(finished.wait(), 2)
    no_output(case)
    assert await case.d.store.get(case.sid) is None
    assert (
        await case.client.post(
            f"/api/v1/sessions/{case.sid}/execution/commands", json=command(case)
        )
    ).status_code == 404


@pytest.mark.asyncio
async def test_no_audience_multiple_approved_products_progress(case_factory, monkeypatch):
    original_source = speech_tests.Source

    class MultiSource(speech_tests.Source):
        def __init__(self):
            super().__init__()
            self.second = original_source()
            self.set.product_ids.append("product-2")
            self.set.brief.product_facts["product-2"] = self.second.set.brief.product_facts[
                "product-1"
            ]

        async def get_approved_version(self, **kwargs):
            source = self.second if kwargs["product_id"] == "product-2" else self
            return ResolvedApprovedScript(
                kwargs["product_id"], source.version.id, source.version.spoken_text
            )

        async def get_script_item(self, **kwargs):
            return self.second.item if kwargs["product_id"] == "product-2" else self.item

        async def get_script_version(self, **kwargs):
            return self.second.version if kwargs["product_id"] == "product-2" else self.version

        async def get_approval(self, **kwargs):
            return self.second.approval if kwargs["product_id"] == "product-2" else self.approval

    monkeypatch.setattr(speech_tests, "Source", MultiSource)
    # Use the original class inside MultiSource, not the patched module alias.
    case = await prepared(case_factory)
    await request(case)
    await until(
        lambda: any(
            e["type"] == "coordinator.speak_finished" and e.get("product_id") == "product-2"
            for e in case.events
        )
    )
    finished = [e for e in case.events if e["type"] == "coordinator.speak_finished"]
    assert sum(e["action"] == "autonomous_opening" for e in finished) == 1
    assert {e.get("product_id") for e in finished} == {"product-1", "product-2"}
    assert not case.llm.started.is_set()
    assert not case.d.director.get_session(case.sid).director.state.rolling_comments


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_write", [1, 2])
async def test_ambiguous_or_failed_start_write_never_reactivates(
    case_factory, monkeypatch, failed_write
):
    case = await prepared(case_factory)
    original = execution._save
    calls = 0

    async def failure(*args):
        nonlocal calls
        calls += 1
        if calls == failed_write:
            raise HTTPException(status_code=503, detail={"code": "session_busy"})
        return await original(*args)

    monkeypatch.setattr(execution, "_save", failure)
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/execution/commands", json=command(case)
    )
    assert response.status_code == 503
    if failed_write == 1:
        no_output(case)
    retry = await request(case)
    assert retry["replayed"] == (failed_write == 2)
    assert retry["outcome"]["status"] == ("accepted" if failed_write == 2 else "applied")
    await until(lambda: case.d.coordinator.opening_media(case.sid))
    assert (
        sum(
            e["type"] == "coordinator.speak_started" and e.get("action") == "autonomous_opening"
            for e in case.events
        )
        == 1
    )


@pytest.mark.asyncio
async def test_stop_racing_start_receipt_does_not_resurrect_session(case_factory, monkeypatch):
    case = await prepared(case_factory)
    entered, release = asyncio.Event(), asyncio.Event()
    original = execution._save

    async def delayed(*args):
        entered.set()
        await release.wait()
        return await original(*args)

    monkeypatch.setattr(execution, "_save", delayed)
    pending = asyncio.create_task(request(case))
    await asyncio.wait_for(entered.wait(), 2)
    stop = asyncio.create_task(case.client.post(f"/api/v1/sessions/{case.sid}/stop"))
    await until(lambda: not case.d.coordinator.has(case.sid))
    release.set()
    await pending
    assert (await stop).status_code == 200
    assert await case.d.store.get(case.sid) is None
    no_output(case)


@pytest.mark.asyncio
async def test_ambiguous_opening_provider_timeout_is_not_blindly_retried(case_factory, monkeypatch):
    case = await prepared(case_factory, cloud=True)
    calls = []

    def ambiguous(*args):
        calls.append(args)
        raise TimeoutError("provider may already have emitted opening")

    monkeypatch.setattr(case.backend, "say", ambiguous)
    await request(case)
    await until(lambda: any(e["type"] == "coordinator.speak_failed" for e in case.events))
    assert (await request(case))["replayed"]
    assert len(calls) == 1
    assert not (await state(case))["first_ai_broadcast"]


@pytest.mark.asyncio
async def test_start_and_media_evidence_keep_existing_auth_boundaries(case_factory):
    case = await prepared(case_factory, mark_ready=False)
    case.d.config.backend_api_token = "fixture-viewer"
    case.d.config.admin_api_token = "fixture-admin"
    url = f"/api/v1/sessions/{case.sid}/execution/commands"
    assert (await case.client.post(url, json=command(case))).status_code == 401
    case.client.headers["Authorization"] = "Bearer fixture-viewer"
    event = dict(
        **identity(case),
        kind="runtime_ready",
        phase="ready",
        sequence=1,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        media_readiness=MEDIA,
    )
    assert (
        await case.client.post(f"/api/v1/sessions/{case.sid}/execution/evidence", json=event)
    ).status_code == 403
    no_output(case)


@pytest.mark.asyncio
async def test_comment_cannot_replace_authorized_start(case_factory):
    case = await prepared(case_factory)
    case.d.coordinator.ingest(case.sid, "hello", "viewer")
    await case.d.coordinator._tick_once(case.sid)
    no_output(case)
    assert case.sid not in case.d.coordinator._activated


@pytest.mark.asyncio
async def test_dependency_loss_after_ready_rejects_start(case_factory):
    case = await prepared(case_factory)
    case.d.engine_manager.tts_load_error = "fixture-dependency-loss"
    result = await request(case)
    assert result["outcome"]["reason_code"] == "dependencies_not_ready"
    no_output(case)


@pytest.mark.asyncio
async def test_start_cannot_replay_another_command_with_same_id(case_factory):
    case = await prepared(case_factory)
    hold = await request(case, command(case, command="hold"))
    assert hold["outcome"]["reason_code"] == "unsupported_capability"
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/execution/commands", json=command(case)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_command_conflict"
    no_output(case)


@pytest.mark.asyncio
async def test_failed_media_write_does_not_claim_first_playable(case_factory, monkeypatch):
    case = await prepared(case_factory)
    await request(case)
    await until(lambda: case.d.coordinator.opening_media(case.sid))
    receipt = case.d.coordinator.opening_media(case.sid)
    before = await state(case)
    event = dict(
        **identity(case),
        kind="first_ai_broadcast",
        phase="warming",
        sequence=2,
        occurred_at=datetime.now(timezone.utc).isoformat(),
        media_readiness=MEDIA,
        opening_turn_id=receipt["opening_turn_id"],
        media_utterance_id=receipt["media_utterance_id"],
        media_evidence_id="playback-fixture-1",
    )
    original = execution._save

    async def failure(*args):
        raise HTTPException(status_code=503, detail={"code": "session_busy"})

    monkeypatch.setattr(execution, "_save", failure)
    url = f"/api/v1/sessions/{case.sid}/execution/evidence"
    assert (await case.client.post(url, json=event)).status_code == 503
    assert await state(case) == before
    monkeypatch.setattr(execution, "_save", original)
    response = await case.client.post(url, json=event)
    assert response.status_code == 200
    assert response.json()["state"]["first_ai_broadcast"]
