"""P0-FB-016 rescue commands through the real HTTP endpoint, Coordinator and
approved-speech fence. Only authoring storage and media/LLM providers are
controlled doubles (see test_approved_speech_active)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from backend.api.v1 import execution as execution_module
from backend.application.director.clustering import Comment
from backend.application.director.decision import Decision
from backend.application.execution_contract import (
    RESCUE_EFFECTS,
    RESCUE_SWITCH,
    Capabilities,
    CommandOutcome,
    ExecutionState,
)
from approved_speech_helpers import TEXT
from . import test_approved_speech_active as speech_tests

case_factory = speech_tests.case_factory
pytestmark = pytest.mark.timeout(30)
RESCUE = ["hold", "resume", "interrupt", "end", "emergency_end"]


@pytest.fixture
def rescue(monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")


def body(case, command, command_id=None, **changes):
    return {
        "tenant_id": "tenant-1",
        "business_session_id": "business-1",
        "runtime_session_id": case.sid,
        "generation": "generation-1",
        "command": command,
        "command_id": command_id or f"{command}-1",
        "actor_id": "owner-1",
        "requested_at": "2026-09-30T00:00:00Z",
        **changes,
    }


async def send(case, command, command_id=None, status=200, **changes):
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/execution/commands",
        json=body(case, command, command_id, **changes),
    )
    assert response.status_code == status, response.text
    return response.json()


async def state(case):
    response = await case.client.get(f"/api/v1/sessions/{case.sid}/execution")
    assert response.status_code == 200, response.text
    return response.json()


async def live(case_factory, phase="selling", marker=True, **kwargs):
    case = await case_factory(**kwargs)
    meta = await case.d.store.get(case.sid)
    meta["execution_contract"].update(phase=phase, runtime_ready=True, sequence=3)
    if marker:  # the API marks a Facebook P0 session at start
        meta["p0_rescue"] = True
    await case.d.store.set(case.sid, meta)
    return case


def turn(case, action="introduce_product", prompt="sell raw facts", **changes: Any) -> Decision:
    return Decision(
        action=action,
        product_id="product-1",
        prompt=prompt,
        revision_token=case.d.director.current_generation_token(case.sid),
        **changes,
    )


async def queued(case, decision):
    coordinator = case.d.coordinator
    coordinator._decision_queue[case.sid].append(decision)
    await coordinator._prepare_turn(case.sid, decision)
    assert decision in coordinator._speech_queue[case.sid]
    return decision


def gated_tts(case):
    """First TTS dispatch blocks mid-utterance until the gate opens."""
    started, gate = threading.Event(), threading.Event()
    original = case.tts.stream_audio

    def slow(chunk, **kwargs):
        started.set()
        assert gate.wait(5), "test failed to open the TTS gate"
        yield from original(chunk, **kwargs)

    case.tts.stream_audio = slow
    return started, gate


def applied(result, command):
    outcome = result["outcome"]
    assert outcome["status"] == "applied", outcome
    assert outcome["reason_code"] == "applied_command"
    assert outcome["effect"] == RESCUE_EFFECTS[command]
    return outcome


def rejected(result, reason):
    assert result["outcome"]["status"] == "rejected", result
    assert result["outcome"]["reason_code"] == reason
    assert result["outcome"]["effect"] is None


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


def spoken(case):
    return [e for e in case.events if e["type"] == "coordinator.speak_started"]


@pytest.mark.asyncio
async def test_rescue_capabilities_are_disabled_until_compatible(case_factory):
    case = await live(case_factory, marker=False)
    before = await state(case)
    assert not set(before["capabilities"]["available"]) & {f"command.{c}" for c in RESCUE}
    for command in RESCUE:
        rejected(await send(case, command), "unsupported_capability")
    assert (await state(case))["state"] == before["state"]


@pytest.mark.asyncio
async def test_hold_at_safe_boundary_lets_current_utterance_finish(case_factory, rescue):
    case = await live(case_factory)
    coordinator = case.d.coordinator
    assert Capabilities.for_session({"p0_rescue": True}).supports(*(f"command.{c}" for c in RESCUE))
    audio = []

    async def publish(window):
        audio.append(window)

    coordinator._audio_window_callback = publish
    started, gate = gated_tts(case)
    current = await queued(case, turn(case))
    coordinator._speech_queue[case.sid].clear()
    speaking = asyncio.create_task(coordinator._maybe_speak(case.sid, current))
    assert await asyncio.to_thread(started.wait, 2)

    outcome = applied(await send(case, "hold"), "hold")
    assert outcome["held"] is True and outcome["sequence"] == 4
    execution = (await state(case))["state"]
    assert execution["hold"]["held"] and execution["hold"]["hold_command_id"] == "hold-1"
    assert execution["phase"] == "selling"  # Hold is a flag, not a phase.

    gate.set()
    assert await speaking
    assert not current.is_cancelled
    assert coordinator._completed_speech[case.sid]["turn_id"] == current.turn_id
    assert coordinator._completed_speech[case.sid]["state"] == "completed"
    calls = list(case.tts.calls)
    assert calls and audio, "the current utterance must reach media"

    # A turn prepared while held stays queued and never starts.
    waiting = await queued(case, turn(case))
    assert await coordinator._maybe_speak(case.sid, waiting) is False
    assert not waiting.is_cancelled
    assert case.tts.calls == calls
    assert [e["turn_id"] for e in spoken(case)] == [current.turn_id]

    # Director tick and Q&A arrival: ingested, never scheduled or voiced.
    coordinator._speech_queue[case.sid].clear()
    coordinator._activated.add(case.sid)
    comment = coordinator.ingest(case.sid, "Bao lâu giao hàng?", "viewer")
    await coordinator._tick_once(case.sid)
    ds = case.d.director.get_session(case.sid)
    assert comment.id in {c.id for c in ds.director.state.rolling_comments}
    assert not coordinator._decision_queue[case.sid] and not coordinator._prepare_tasks[case.sid]
    say = await case.client.post(
        f"/api/v1/sessions/{case.sid}/say", json={"text": TEXT, "generate": False}
    )
    assert say.status_code == 409, say.text
    assert say.json()["error"]["code"] == "held"
    assert case.tts.calls == calls


@pytest.mark.asyncio
async def test_resume_exactly_once_and_out_of_order(case_factory, rescue):
    case = await live(case_factory)
    rejected(await send(case, "resume", "resume-0"), "invalid_lifecycle_state")
    applied(await send(case, "hold"), "hold")
    rejected(await send(case, "hold", "hold-2"), "invalid_lifecycle_state")
    first = await send(case, "resume")
    outcome = applied(first, "resume")
    assert outcome["held"] is False and not first["replayed"]
    replay = await send(case, "resume")
    assert replay["replayed"] and replay["outcome"] == outcome
    rejected(await send(case, "resume", "resume-2"), "invalid_lifecycle_state")
    hold = (await state(case))["state"]["hold"]
    assert not hold["held"] and hold["resumed_command_id"] == "resume-1"
    assert case.d.approved_speech.blocked(case.sid) is None


@pytest.mark.asyncio
async def test_resume_expires_queued_qa_by_original_time(case_factory, rescue):
    case = await live(case_factory)
    coordinator = case.d.coordinator
    ds = case.d.director.get_session(case.sid)
    horizon = ds.director.cfg.selection_window_sec
    old = Comment(text="old", embedding=[0.0], t=ds.now() - horizon - 5)
    fresh = Comment(text="fresh", embedding=[0.0], t=ds.now())
    ds.director.state.rolling_comments.extend([old, fresh])
    applied(await send(case, "hold"), "hold")
    stale = turn(case, action="answer_cluster", cluster_member_ids=(old.id,))
    pruned = turn(case, action="answer_fact", cluster_member_ids=("gone",))
    kept = turn(case, action="answer_cluster", cluster_member_ids=(fresh.id,))
    selling = turn(case)
    for decision in (stale, pruned, kept, selling):
        coordinator._speech_queue[case.sid].append(decision)
    applied(await send(case, "resume"), "resume")
    assert list(coordinator._speech_queue[case.sid]) == [kept, selling]
    assert stale.is_cancelled and pruned.is_cancelled
    reasons = {
        item["turn_id"]: item.get("cancellation_reason")
        for item in coordinator._completed_history[case.sid]
    }
    assert reasons[stale.turn_id] == reasons[pruned.turn_id] == "hard_expired"
    assert not case.tts.calls


@pytest.mark.asyncio
async def test_popped_qa_hard_expires_across_hold_resume_during_revalidation(case_factory, rescue):
    """A turn popped into _maybe_speak is in neither queue; Resume cannot expire it."""
    case = await live(case_factory)
    coordinator = case.d.coordinator
    ds = case.d.director.get_session(case.sid)
    comment = Comment(text="q", embedding=[0.0], t=ds.now())
    ds.director.state.rolling_comments.append(comment)
    popped = await queued(case, turn(case))
    coordinator._speech_queue[case.sid].clear()
    popped.action, popped.cluster_member_ids = "answer_cluster", (comment.id,)
    entered, release = asyncio.Event(), asyncio.Event()
    original = case.d.approved_speech.revalidate

    async def slow(speech, **kwargs):
        entered.set()
        await release.wait()
        return await original(speech, **kwargs)

    case.d.approved_speech.revalidate = slow
    speaking = asyncio.create_task(coordinator._maybe_speak(case.sid, popped))
    await asyncio.wait_for(entered.wait(), 2)
    applied(await send(case, "hold"), "hold")
    comment.t = ds.now() - ds.director.cfg.selection_window_sec - 5  # aged while held
    applied(await send(case, "resume"), "resume")
    release.set()
    assert await asyncio.wait_for(speaking, 5)
    assert popped.is_cancelled and not case.tts.calls
    reasons = {
        i["turn_id"]: i.get("cancellation_reason") for i in coordinator._completed_history[case.sid]
    }
    assert reasons[popped.turn_id] == "hard_expired"


@pytest.mark.asyncio
async def test_stale_envelope_across_hold_is_not_spoken(case_factory, rescue):
    case = await live(case_factory)
    waiting = await queued(case, turn(case))
    applied(await send(case, "hold"), "hold")
    case.source.item.approved_version_id = "stale"
    applied(await send(case, "resume"), "resume")
    assert await case.d.coordinator._maybe_speak(case.sid, waiting)
    assert waiting.is_cancelled
    assert not case.tts.calls
    assert any(e["type"] == "speech.content_rejected" for e in case.events)


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True])
async def test_interrupt_cancels_playback_and_preserves_hold(case_factory, rescue, held):
    case = await live(case_factory)
    coordinator = case.d.coordinator
    audio = []

    async def publish(window):
        audio.append(window)

    coordinator._audio_window_callback = publish
    started, gate = gated_tts(case)
    current = await queued(case, turn(case))
    coordinator._speech_queue[case.sid].clear()
    waiting = await queued(case, turn(case))
    speaking = asyncio.create_task(coordinator._maybe_speak(case.sid, current))
    assert await asyncio.to_thread(started.wait, 2)
    if held:
        applied(await send(case, "hold"), "hold")
    outcome = applied(await send(case, "interrupt"), "interrupt")
    assert outcome["held"] is held
    gate.set()  # late TTS completion after the command
    await speaking
    assert current.is_cancelled and waiting.is_cancelled
    assert not coordinator._speech_queue[case.sid]
    assert not audio, "late audio reached media after interrupt"
    assert (await state(case))["state"]["hold"]["held"] is held
    assert (await state(case))["state"]["phase"] == "selling"


@pytest.mark.asyncio
async def test_hold_start_fence_is_set_before_held_is_persisted(case_factory, rescue, monkeypatch):
    case = await live(case_factory)
    seen = {}
    original = execution_module._save

    async def spy(store, sid, meta, fence):
        seen["blocked_before_persist"] = case.d.approved_speech.blocked(sid)
        await original(store, sid, meta, fence)

    monkeypatch.setattr(execution_module, "_save", spy)
    applied(await send(case, "hold"), "hold")
    assert seen["blocked_before_persist"] == "held"


@pytest.mark.asyncio
async def test_failed_hold_persist_restores_the_start_fence(case_factory, rescue, monkeypatch):
    case = await live(case_factory)

    async def boom(store, sid, meta, fence):
        raise RuntimeError("store down")

    monkeypatch.setattr(execution_module, "_save", boom)
    with pytest.raises(RuntimeError):
        await send(case, "hold")
    assert case.d.approved_speech.blocked(case.sid) is None


@pytest.mark.asyncio
async def test_end_has_no_spoken_closing_and_ends_immediately(case_factory, rescue):
    """F4 truth: End at P0 is an immediate ending with no closing speech."""
    case = await live(case_factory)
    applied(await send(case, "end"), "end")
    await until(lambda: any(e.get("type") == "execution.phase_changed" for e in case.events))
    assert (await state(case))["state"]["phase"] == "ending"
    assert not case.tts.calls and not spoken(case)


@pytest.mark.asyncio
async def test_lost_closing_task_is_recovered_on_execution_read(case_factory, rescue):
    """A restart loses the in-process closing task; the next read completes it."""
    case = await live(case_factory, phase="closing")
    assert execution_module._closing_tasks.get(case.sid) is None
    assert (await state(case))["state"]["phase"] == "ending"
    assert (await state(case))["state"]["phase"] == "ending"


@pytest.mark.asyncio
async def test_end_runs_closing_then_reports_ending(case_factory, rescue):
    case = await live(case_factory)
    coordinator = case.d.coordinator
    waiting = await queued(case, turn(case))
    applied(await send(case, "hold"), "hold")
    outcome = applied(await send(case, "end"), "end")
    assert outcome["held"] is False
    assert waiting.is_cancelled
    await until(lambda: any(e.get("type") == "execution.phase_changed" for e in case.events))
    execution = (await state(case))["state"]
    assert execution["phase"] == "ending"
    assert execution["sequence"] == outcome["sequence"] + 1
    assert execution["hold"]["resumed_command_id"] == "end-1"
    # No new product/Q&A after End; late commands are typed rejections.
    assert await coordinator._maybe_speak(case.sid, turn(case)) is False
    for command in ("hold", "end", "resume"):
        rejected(await send(case, command, f"{command}-late"), "invalid_lifecycle_state")
    assert not case.tts.calls and not spoken(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase,held", [("selling", False), ("closing", False), ("selling", True)])
async def test_emergency_end_skips_closing(case_factory, rescue, phase, held):
    case = await live(case_factory, phase=phase)
    waiting = await queued(case, turn(case))
    if held:
        applied(await send(case, "hold"), "hold")
    outcome = applied(await send(case, "emergency_end"), "emergency_end")
    assert outcome["held"] is False
    execution = (await state(case))["state"]
    assert execution["phase"] == "ending" and execution["terminal_reason"] is None
    assert waiting.is_cancelled
    assert case.d.approved_speech.blocked(case.sid) == "ending"
    await asyncio.sleep(0.05)
    assert (await state(case))["state"]["phase"] == "ending"
    rejected(await send(case, "emergency_end", "again"), "invalid_lifecycle_state")
    assert not case.tts.calls and not spoken(case)


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["interrupt", "end", "emergency_end"])
async def test_late_generated_completion_is_fenced_after_command(case_factory, rescue, command):
    case = await live(case_factory)
    case.llm.release.clear()
    late = turn(case, action="answer_cluster", prompt="Bao lâu giao hàng?")
    case.d.coordinator._decision_queue[case.sid].append(late)
    pending = asyncio.create_task(case.d.coordinator._prepare_turn(case.sid, late))
    assert await asyncio.to_thread(case.llm.started.wait, 2)
    applied(await send(case, command), command)
    case.llm.release.set()
    await pending
    await case.d.coordinator._maybe_speak(case.sid, late)
    assert late.is_cancelled
    assert not case.tts.calls and not getattr(case.backend, "calls", [])


@pytest.mark.asyncio
async def test_terminal_stale_duplicate_and_conflict(case_factory, rescue):
    case = await live(case_factory, phase="ended")
    rejected(await send(case, "hold"), "already_terminal")
    case = await live(case_factory)
    rejected(await send(case, "hold", "old", generation="generation-0"), "stale_generation")
    first = await send(case, "interrupt")
    replay = await send(case, "interrupt")
    assert replay["replayed"] and replay["outcome"] == first["outcome"]
    conflict = await send(case, "hold", "interrupt-1", status=409)
    assert conflict["error"]["code"] == "duplicate_command_conflict"
    assert (await state(case))["state"]["sequence"] == first["outcome"]["sequence"]


@pytest.mark.asyncio
async def test_legacy_interrupt_on_p0_uses_execution_command(case_factory, rescue):
    case = await live(case_factory)
    before = case.d.approved_speech._epochs.get(case.sid, 0)
    response = await case.client.post(f"/api/v1/sessions/{case.sid}/interrupt")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "use_execution_command"
    assert case.d.approved_speech._epochs.get(case.sid, 0) == before


@pytest.mark.asyncio
async def test_unmarked_p0_session_never_sees_rescue_with_switch_on(case_factory, rescue):
    """F1: a TikTok/legacy-marked session is unchanged even with the Runtime switch on."""
    case = await live(case_factory, marker=False)
    caps = (await state(case))["capabilities"]["available"]
    assert not {f"command.{c}" for c in RESCUE} & set(caps)
    before = (await state(case))["state"]
    for command in RESCUE:
        rejected(await send(case, command), "unsupported_capability")
    assert (await state(case))["state"] == before
    assert case.d.approved_speech.blocked(case.sid) is None
    response = await case.client.post(f"/api/v1/sessions/{case.sid}/interrupt")
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_marker_is_the_only_gate_after_start_switch_off(case_factory, monkeypatch):
    """Codex r3 F1: flipping the process switch off after start changes nothing."""
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    case = await live(case_factory)
    monkeypatch.setenv(RESCUE_SWITCH, "0")
    caps = (await state(case))["capabilities"]["available"]
    assert {f"command.{c}" for c in RESCUE} <= set(caps)
    assert Capabilities.for_session({"p0_rescue": True}).supports("command.interrupt")
    applied(await send(case, "hold"), "hold")
    response = await case.client.post(f"/api/v1/sessions/{case.sid}/interrupt")
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_marker_is_the_only_gate_after_start_switch_on(case_factory, monkeypatch):
    """Flipping the switch on after start does not enable an unmarked session."""
    monkeypatch.setenv(RESCUE_SWITCH, "0")
    case = await live(case_factory, marker=False)
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    assert not {f"command.{c}" for c in RESCUE} & set(
        (await state(case))["capabilities"]["available"]
    )
    rejected(await send(case, "hold"), "unsupported_capability")


@pytest.mark.asyncio
async def test_legacy_stop_on_marked_session_is_internal_only(case_factory):
    """Codex r3 F2: viewer-plane /stop cannot bypass End/Emergency on a marked session."""
    case = await live(case_factory)
    case.d.config.admin_api_token = "admin-secret"
    url = f"/api/v1/sessions/{case.sid}/stop"
    for headers in ({}, {"X-Livento-Internal-Cleanup": "wrong"}):
        response = await case.client.post(url, headers=headers)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "use_execution_command"
    assert await case.d.store.get(case.sid) is not None
    ok = await case.client.post(url, headers={"X-Livento-Internal-Cleanup": "admin-secret"})
    assert ok.status_code == 200, ok.text
    assert await case.d.store.get(case.sid) is None


@pytest.mark.asyncio
async def test_legacy_stop_on_unmarked_session_is_unchanged(case_factory):
    case = await live(case_factory, marker=False)
    case.d.config.admin_api_token = "admin-secret"
    response = await case.client.post(f"/api/v1/sessions/{case.sid}/stop")
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_disconnect_retry_returns_recorded_truth_without_reapplying(case_factory, rescue):
    case = await live(case_factory)
    results = await asyncio.gather(*(send(case, "hold") for _ in range(6)))
    assert sum(not r["replayed"] for r in results) == 1
    assert all(r["outcome"] == results[0]["outcome"] for r in results)
    assert (await state(case))["state"]["sequence"] == 4


def test_shared_rescue_fixture_matches_python_contract():
    """Same JSON fixture as Livento-API pkg/executioncontract/testdata."""
    path = Path(__file__).parents[1] / "fixtures" / "rescue_command_v1.json"
    fixture = json.loads(path.read_text(encoding="utf-8"))
    assert fixture["effects"] == RESCUE_EFFECTS
    assert fixture["capabilities"] == [f"command.{c}" for c in RESCUE]
    for raw in fixture["outcomes"]:
        outcome = CommandOutcome.model_validate(raw)
        assert (outcome.effect is not None) == (outcome.status == "applied")
        if outcome.status == "applied":
            assert outcome.effect == RESCUE_EFFECTS[outcome.command]
            assert outcome.sequence and outcome.sequence > 0
        else:
            assert outcome.reason_code in fixture["rejection_reasons"]
    state = ExecutionState.model_validate(fixture["held_state"])
    assert state.hold.held and state.hold.hold_command_id == "cmd-hold-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("env", ["dev", "prod"])
async def test_empty_admin_token_never_accepts_cleanup_header(case_factory, env):
    """R4-R3: viewer-token holders cannot stop a marked live session, dev or prod."""
    case = await live(case_factory)
    cfg = case.d.config
    cfg.app_env, cfg.backend_api_token, cfg.admin_api_token = env, "viewer-secret", ""
    for hdr in ({}, {"X-Livento-Internal-Cleanup": ""}, {"X-Livento-Internal-Cleanup": "x"}):
        r = await case.client.post(
            f"/api/v1/sessions/{case.sid}/stop",
            headers={"Authorization": "Bearer viewer-secret", **hdr},
        )
        assert r.status_code == 409, r.text
    assert await case.d.store.get(case.sid) is not None
