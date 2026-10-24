"""Live-follow facts: position, comment->cluster membership, turn outcomes, recovery.

The recorder tests drive the fact model directly; the composition tests run the real
coordinator + approved-speech boundary with only providers doubled.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from approved_speech_helpers import CLAIM, MultiSource
from backend.application.director.decision import Decision
from backend.application.live_follow import LiveFollowRecorder
from backend.application.live_follow.recorder import QUEUE_MAX, normalize_reason, topic_code
from backend.application.reducer import AcceptedComment

from . import test_approved_speech_active as speech_tests
from .test_approved_speech_active import decision, open_session, play_next, prepare

case_factory = speech_tests.case_factory

UNITS = [f"Phần {i} giới thiệu sản phẩm." for i in range(10)]
UNITS[2] += " " + CLAIM
ENV = "RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED"


# ---------------------------------------------------------------- recorder level


def _ds(units=tuple(UNITS), next_unit=0):
    product = SimpleNamespace(product_id="p1", units=units, next_unit=next_unit)
    state = SimpleNamespace(current_product=lambda: product, products=[product])
    return SimpleNamespace(director=SimpleNamespace(state=state), approved_envelope=None), product


def _recorder():
    sent: list[dict] = []

    async def emit(_sid, event):
        sent.append(event)

    recorder = LiveFollowRecorder(emit=emit, enabled=True)
    recorder.begin("s")
    recorder.bind(
        "s",
        {
            "tenant_id": "t",
            "business_session_id": "b",
            "runtime_session_id": "s",
            "generation": "g1",
        },
    )
    return recorder, sent


def _qa(turn_id="turn-1", members=("c1", "c2"), cluster="cl-1", topic="price", attempt=0):
    return Decision(
        action="answer_cluster",
        product_id="p1",
        topic=topic,
        cluster_member_ids=tuple(members),
        source_cluster_id=cluster,
        turn_id=turn_id,
        attempt=attempt,
    )


def _script(index, turn_id=None):
    return Decision(
        action="sell_product",
        product_id="p1",
        unit_index=index,
        turn_id=turn_id or f"unit-{index}",
    )


def _prov(cid):
    return (
        f"evt-{cid}",
        {
            "platform": "facebook",
            "connected_account_id": "acc",
            "external_session_id": "live",
            "source_message_id": f"m/{cid}",
        },
        time.time(),
    )


async def _drain(recorder, sent):
    await recorder.flush("s")
    return list(sent)


def _types(events):
    return [e["type"].removeprefix("live_follow.") for e in events]


def _members(recorder, mapping, now=None):
    recorder.sync_membership(
        "s",
        mapping,
        _prov,
        now=now or time.time(),
        horizon_sec=75.0,
        force=True,
    )


@pytest.mark.asyncio
async def test_disabled_recorder_records_nothing():
    sent: list[dict] = []

    async def emit(_sid, event):
        sent.append(event)

    recorder = LiveFollowRecorder(emit=emit, enabled=False)
    recorder.begin("s")
    ds, _ = _ds()
    recorder.turn_selected("s", _qa(), ds)
    recorder.speak_started("s", _qa(), ds)
    await asyncio.sleep(0)
    assert sent == [] and recorder.snapshot("s") == {"schema_version": 1, "enabled": False}


@pytest.mark.asyncio
async def test_envelope_is_stable_ordered_and_scoped():
    recorder, sent = _recorder()
    ds, _ = _ds()
    recorder.turn_selected("s", _qa(), ds)
    recorder.speak_started("s", _qa(), ds)
    events = await _drain(recorder, sent)
    assert [e["follow_sequence"] for e in events] == list(range(1, len(events) + 1))
    first = events[0]
    assert (first["tenant_id"], first["business_session_id"], first["runtime_session_id"]) == (
        "t",
        "b",
        "s",
    )
    assert first["generation"] == "g1" and first["schema_version"] == 1
    assert first["source_event_id"].endswith(":selected:turn-1")
    assert len({e["source_event_id"] for e in events}) == len(events)


@pytest.mark.asyncio
async def test_turn_selected_freezes_members_and_late_member_never_inherits_answered():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price"), "c2": ("cl-1", "price")})
    turn = _qa()
    recorder.turn_selected("s", turn, ds)
    _members(recorder, {"c1": ("cl-1", "price"), "c2": ("cl-1", "price"), "c3": ("cl-1", "price")})
    recorder.speak_started("s", turn, ds)
    recorder.turn_completed("s", turn, ds, response_kind="answer", completed_at=time.time())
    events = await _drain(recorder, sent)
    selected = next(e for e in events if e["type"].endswith("cluster.turn_selected"))
    assert selected["member_ids"] == ["c1", "c2"] and selected["member_count"] == 2
    assert selected["topic_code"] == "price" and selected["cluster_id"] == "cl-1"
    done = next(e for e in events if e["type"].endswith("turn.completed"))
    assert (done["response_kind"], done["coverage_scope"]) == ("answer", "cluster")
    assert done["member_ids_sha256"] == selected["member_ids_sha256"]
    states = {c["comment_id"]: c for c in recorder.snapshot("s")["comments"]}
    assert (states["c1"]["state"], states["c1"]["final"]) == ("answered", True)
    assert states["c3"]["state"] == "pending"


@pytest.mark.asyncio
async def test_selected_is_not_answered_until_completed():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price")})
    recorder.turn_selected("s", _qa(members=("c1",)), ds)
    recorder.speak_started("s", _qa(members=("c1",)), ds)
    await _drain(recorder, sent)
    assert recorder.snapshot("s")["comments"][0]["state"] == "selected"


@pytest.mark.asyncio
async def test_completed_before_selected_is_ignored_and_terminal_is_first_wins():
    recorder, sent = _recorder()
    ds, _ = _ds()
    turn = _qa(members=("c1",))
    recorder.turn_completed("s", turn, ds, response_kind="answer", completed_at=time.time())
    recorder.turn_selected("s", turn, ds)
    recorder.turn_completed("s", turn, ds, response_kind="deflection", completed_at=time.time())
    recorder.turn_ended("s", turn, ds, status="failed", reason="failed")
    events = await _drain(recorder, sent)
    terminals = [e for e in events if ".turn." in e["type"]]
    assert [(e["type"].rsplit(".", 1)[1], e["response_kind"]) for e in terminals] == [
        ("completed", "deflection")
    ]


@pytest.mark.asyncio
async def test_cancelled_and_failed_turn_make_members_wait_not_answered():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price"), "c2": ("cl-1", "price")})
    turn = _qa()
    recorder.turn_selected("s", turn, ds)
    recorder.turn_ended("s", turn, ds, status="cancelled", reason="interrupt")
    retry = _qa(turn_id="turn-2")
    recorder.turn_selected("s", retry, ds)
    recorder.turn_ended("s", retry, ds, status="failed", reason="failed")
    events = await _drain(recorder, sent)
    deferred = [e for e in events if e["type"].endswith("comment.deferred")]
    assert [(e["reason_code"], e["final"]) for e in deferred] == [
        ("turn_cancelled", False),
        ("turn_failed", False),
    ]
    assert {c["state"] for c in recorder.snapshot("s")["comments"]} == {"deferred"}


@pytest.mark.asyncio
async def test_member_that_left_the_store_is_final_when_its_turn_dies():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price")})
    turn = _qa(members=("c1",))
    recorder.turn_selected("s", turn, ds)
    _members(recorder, {})
    recorder.turn_ended("s", turn, ds, status="failed", reason="failed")
    events = await _drain(recorder, sent)
    skipped = [e for e in events if e["type"].endswith("comment.skipped")]
    assert [(e["comment_ids"], e["reason_code"], e["final"]) for e in skipped] == [
        (["c1"], "turn_failed", True)
    ]


@pytest.mark.asyncio
async def test_cluster_assignment_has_revisions_and_reassign_and_remove():
    recorder, sent = _recorder()
    _members(recorder, {"c1": ("cl-1", "price"), "c2": ("cl-1", "price")})
    _members(recorder, {"c1": ("cl-2", "price"), "c2": ("cl-1", "price")})
    _members(recorder, {"c2": ("cl-1", "price")}, now=time.time())
    events = [e for e in await _drain(recorder, sent) if e["type"].endswith("cluster_assigned")]
    revisions = [e["membership_revision"] for e in events]
    assert revisions == sorted(set(revisions)) and len(revisions) == 3
    first = events[0]["changes"][0]
    assert first["comment_key"] == "facebook/acc/live/m%2Fc1" and first["event_id"] == "evt-c1"
    assert events[1]["changes"] == [
        {
            "comment_id": "c1",
            "event_id": "evt-c1",
            "comment_key": "facebook/acc/live/m%2Fc1",
            "cluster_id": "cl-2",
            "previous_cluster_id": "cl-1",
            "change": "reassigned",
            "cluster_revision": 2,
        }
    ]
    assert events[2]["changes"][0]["change"] == "removed"


@pytest.mark.asyncio
async def test_answered_member_keeps_answer_when_cluster_expires_or_reassigns():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price")})
    turn = _qa(members=("c1",))
    recorder.turn_selected("s", turn, ds)
    recorder.turn_completed("s", turn, ds, response_kind="answer", completed_at=time.time())
    _members(recorder, {"c1": ("cl-9", "price")})
    _members(recorder, {}, now=time.time() + 500)
    await _drain(recorder, sent)
    comment = recorder.snapshot("s")["comments"][0]
    assert (comment["state"], comment["final"]) == ("answered", True)


@pytest.mark.asyncio
async def test_unanswered_comment_gets_final_reason_when_it_leaves_the_store():
    recorder, sent = _recorder()
    # "old" aged past the horizon; "new" was cut by a bound while still fresh.
    old_ts = time.time() - 300

    def prov(cid):
        record = _prov(cid)
        return (record[0], record[1], old_ts if cid == "old" else time.time())

    both = {"old": ("cl-1", "price"), "new": ("cl-2", "price")}
    recorder.sync_membership("s", both, prov, now=time.time(), horizon_sec=75.0, force=True)
    recorder.sync_membership(
        "s", {}, lambda _c: None, now=time.time(), horizon_sec=75.0, force=True
    )
    events = await _drain(recorder, sent)
    reasons = {
        e["comment_ids"][0]: (e["reason_code"], e["final"])
        for e in events
        if e["type"].endswith("comment.skipped")
    }
    assert reasons == {"old": ("expired", True), "new": ("capacity_exceeded", True)}


@pytest.mark.asyncio
async def test_non_actionable_intent_is_final_but_unknown_is_not():
    recorder, sent = _recorder()
    _members(recorder, {"spam1": ("cl-1", "spam"), "q1": ("cl-2", "unknown")})
    events = await _drain(recorder, sent)
    skipped = [e for e in events if e["type"].endswith("comment.skipped")]
    assert [(e["comment_ids"], e["reason_code"], e["final"]) for e in skipped] == [
        (["spam1"], "non_actionable", True)
    ]


@pytest.mark.asyncio
async def test_cooldown_tick_is_deferred_not_final_and_reported_once():
    recorder, sent = _recorder()
    _members(recorder, {"c1": ("cl-1", "price")})
    notes = [("topic_cooldown", ("c1",)), ("size_gate", ("c9",))]
    recorder.note_tick_skips("s", notes)
    recorder.note_tick_skips("s", notes)
    events = await _drain(recorder, sent)
    deferred = [e for e in events if e["type"].endswith("comment.deferred")]
    assert [(e["comment_ids"], e["reason_code"], e["final"]) for e in deferred] == [
        (["c1"], "topic_recently_answered", False)
    ]
    assert not any(e["type"].endswith("comment.skipped") for e in events)


@pytest.mark.asyncio
async def test_closing_makes_open_comments_final_and_answered_stay():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price"), "c2": ("cl-2", "price")})
    turn = _qa(members=("c1",))
    recorder.turn_selected("s", turn, ds)
    recorder.turn_completed("s", turn, ds, response_kind="answer", completed_at=time.time())
    recorder.close_pending("s", "session_closing")
    recorder.close_pending("s", "session_closing")
    events = await _drain(recorder, sent)
    skipped = [e for e in events if e["type"].endswith("comment.skipped")]
    assert [(e["comment_ids"], e["reason_code"], e["final"]) for e in skipped] == [
        (["c2"], "session_closing", True)
    ]


def test_unknown_reason_is_never_final_and_exposes_no_text():
    assert normalize_reason("Traceback: boom at /srv/x.py", True) == ("unknown", False)
    assert normalize_reason("expired", True) == ("expired", True)
    assert topic_code("price") == "price" and topic_code("weird") == "product_question"


@pytest.mark.asyncio
async def test_intake_rejection_reports_event_id_only():
    recorder, sent = _recorder()
    recorder.rejected_before_runtime("s", "evt-9", "safety_filtered")
    event = (await _drain(recorder, sent))[0]
    assert (event["event_ids"], event["comment_ids"], event["final"]) == (["evt-9"], [], True)


@pytest.mark.asyncio
async def test_queue_is_bounded_and_reports_drops_when_nobody_drains():
    recorder = LiveFollowRecorder(emit=None, enabled=True)
    recorder.begin("s")
    for index in range(QUEUE_MAX + 25):
        recorder.rejected_before_runtime("s", f"e{index}", "expired")
    snapshot = recorder.snapshot("s")
    assert snapshot["dropped_events"] == 25 and snapshot["watermark"] == QUEUE_MAX + 25


@pytest.mark.asyncio
async def test_emit_failure_never_raises_into_the_caller():
    async def broken(_sid, _event):
        raise RuntimeError("hub down")

    recorder = LiveFollowRecorder(emit=broken, enabled=True)
    recorder.begin("s")
    ds, _ = _ds()
    recorder.turn_selected("s", _qa(), ds)
    await recorder.flush("s")
    assert recorder.snapshot("s")["watermark"] >= 1


@pytest.mark.asyncio
async def test_events_and_snapshot_carry_no_comment_text_or_names():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price")})
    recorder.turn_selected("s", _qa(members=("c1",)), ds)
    recorder.turn_completed("s", _qa(members=("c1",)), ds, response_kind="answer", completed_at=1.0)
    blob = json.dumps([*sent, recorder.snapshot("s")])
    assert "giới thiệu sản phẩm" not in blob  # unit text is hashed, never copied


@pytest.mark.asyncio
async def test_position_keeps_the_script_cursor_during_qa_and_advances_only_on_completion():
    recorder, sent = _recorder()
    ds, product = _ds()
    unit3 = _script(3)
    recorder.turn_selected("s", unit3, ds)
    recorder.speak_started("s", unit3, ds)
    qa = _qa()
    recorder.turn_selected("s", qa, ds)  # a Q&A queued while unit 3 speaks: no position change
    positions = [e["position"] for e in await _drain(recorder, sent) if "position" in e]
    assert positions[-1]["phase"] == "speaking" and positions[-1]["unit_index"] == 3
    product.next_unit = 3
    recorder.turn_completed("s", unit3, ds, response_kind="script", completed_at=1.0)
    recorder.speak_started("s", qa, ds)
    product.next_unit = 4  # unit 3 done
    recorder.turn_completed("s", unit3, ds, response_kind="script", completed_at=1.0)
    answering = [e["position"] for e in await _drain(recorder, sent) if "position" in e]
    qa_position = next(p for p in answering if p["phase"] == "answering_comment")
    assert qa_position["topic_code"] == "price" and qa_position["turn_kind"] == "qa"
    assert qa_position["script_cursor"]["next_unit_index"] == 3  # held: Q&A did not advance it
    assert qa_position["unit_text_sha256"] is None
    last = recorder.snapshot("s")["position"]
    assert last["script_cursor"]["next_unit_index"] == 4 and last["phase"] == "preparing"


@pytest.mark.asyncio
async def test_hold_and_resume_are_positions():
    recorder, sent = _recorder()
    ds, _ = _ds()
    recorder.hold("s", ds, "held")
    recorder.hold("s", ds, "held")
    recorder.hold("s", ds, None)
    phases = [e["position"]["phase"] for e in await _drain(recorder, sent)]
    assert phases == ["holding", "preparing"]


# ------------------------------------------------------- review regressions (PR #124)


@pytest.mark.asyncio
async def test_reattach_never_emits_events_of_the_old_generation():
    gate = asyncio.Event()
    sent: list[dict] = []

    async def emit(_sid, event):
        await gate.wait()
        sent.append(event)

    recorder = LiveFollowRecorder(emit=emit, enabled=True)
    recorder.begin("s")
    old_epoch = recorder._sessions["s"].epoch
    for index in range(5):
        recorder.rejected_before_runtime("s", f"old{index}", "expired")
    await asyncio.sleep(0)  # the drain task is now blocked inside emit
    recorder.retire("s")
    recorder.begin("s")
    recorder.rejected_before_runtime("s", "new0", "expired")
    gate.set()
    await recorder.flush("s")
    await asyncio.sleep(0.01)
    old = [e for e in sent if e["stream_epoch"] == old_epoch]
    assert len(old) <= 1  # only the event already inside emit when the session was replaced
    assert [e["event_ids"] for e in sent if e["stream_epoch"] != old_epoch] == [["new0"]]


@pytest.mark.asyncio
async def test_retired_backlog_is_dropped_after_the_grace_period(monkeypatch):
    from backend.application.live_follow import recorder as module

    monkeypatch.setattr(module, "RETIRE_GRACE_SEC", 0.0)
    sent: list[dict] = []
    gate = asyncio.Event()

    async def emit(_sid, event):
        await gate.wait()
        sent.append(event)

    recorder = LiveFollowRecorder(emit=emit, enabled=True)
    recorder.begin("s")
    for index in range(50):
        recorder.rejected_before_runtime("s", f"e{index}", "expired")
    await asyncio.sleep(0)
    recorder.retire("s")
    gate.set()
    await asyncio.sleep(0.01)
    assert len(sent) <= 1


@pytest.mark.asyncio
async def test_comment_arriving_after_closing_is_finalized():
    recorder, sent = _recorder()
    _members(recorder, {"c1": ("cl-1", "price")})
    recorder.close_pending("s", "session_closing")
    _members(recorder, {"c1": ("cl-1", "price"), "late": ("cl-1", "price")})
    recorder.close_pending("s", "session_closing")  # a later tick: nothing new to say
    events = await _drain(recorder, sent)
    skipped = [
        (e["comment_ids"], e["reason_code"]) for e in events if e["type"].endswith("skipped")
    ]
    assert skipped == [(["c1"], "session_closing"), (["late"], "session_closing")]


@pytest.mark.asyncio
async def test_real_reducer_expiry_is_expired_not_capacity_exceeded():
    from backend.application.director.embeddings import HashingEmbedder
    from backend.application.reducer import FastReducer

    recorder, sent = _recorder()
    clock = [1000.0]
    reducer = FastReducer(embedder=HashingEmbedder(), now_fn=lambda: clock[0])
    reducer.membership_observer = lambda sid, store, now: recorder.sync_membership(
        sid,
        store.membership(),
        lambda cid: reducer.comment_provenance(sid, cid),
        now=now,
        horizon_sec=store.config.rolling_horizon_sec,
        force=True,
    )

    def accept(cid, text, ts):
        reducer.notify_new_events(
            "s", comment=AcceptedComment(event_id=f"e-{cid}", comment_id=cid, text=text, ts=ts)
        )

    accept("old", "giá bao nhiêu vậy shop", clock[0])
    await reducer.run_once("s", clock[0])
    clock[0] += 76
    accept("new", "còn hàng không shop", clock[0])
    await reducer.run_once("s", clock[0])
    events = await _drain(recorder, sent)
    reasons = [
        (e["comment_ids"], e["reason_code"])
        for e in events
        if e["type"].endswith("comment.skipped")
    ]
    assert reasons == [(["old"], "expired")]


@pytest.mark.asyncio
async def test_two_failures_of_the_same_comment_have_distinct_source_event_ids():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price")})
    for turn_id in ("t1", "t2"):
        turn = _qa(turn_id=turn_id, members=("c1",))
        recorder.turn_selected("s", turn, ds)
        recorder.turn_ended("s", turn, ds, status="failed", reason="failed")
    events = await _drain(recorder, sent)
    deferred = [e for e in events if e["type"].endswith("comment.deferred")]
    assert len(deferred) == 2
    assert len({e["source_event_id"] for e in deferred}) == 2
    assert len({e["source_event_id"] for e in events}) == len(events)


@pytest.mark.asyncio
async def test_new_epoch_does_not_reuse_source_event_ids():
    first, sent_a = _recorder()
    first.rejected_before_runtime("s", "e1", "expired")
    a = (await _drain(first, sent_a))[0]
    second, sent_b = _recorder()
    second.rejected_before_runtime("s", "e1", "expired")
    b = (await _drain(second, sent_b))[0]
    assert a["source_event_id"] != b["source_event_id"]


@pytest.mark.asyncio
async def test_turn_selected_after_terminal_keeps_outcome_and_members():
    recorder, sent = _recorder()
    ds, _ = _ds()
    _members(recorder, {"c1": ("cl-1", "price")})
    turn = _qa(members=("c1",))
    recorder.turn_selected("s", turn, ds)
    recorder.turn_completed("s", turn, ds, response_kind="answer", completed_at=time.time())
    recorder.turn_selected("s", _qa(members=("c1", "late")), ds)  # replay by the coordinator
    events = await _drain(recorder, sent)
    assert _types(events).count("cluster.turn_selected") == 1
    entry = recorder.snapshot("s")["turns"][0]
    assert (entry["status"], entry["outcome_revision"], entry["member_ids"]) == (
        "completed",
        1,
        ["c1"],
    )
    states = {c["comment_id"]: c["state"] for c in recorder.snapshot("s")["comments"]}
    assert states == {"c1": "answered"}


@pytest.mark.asyncio
async def test_long_session_state_stays_bounded():
    recorder = LiveFollowRecorder(emit=None, enabled=True)
    recorder.begin("s")
    state = recorder._sessions["s"]
    live: dict[str, tuple[str, str]] = {}
    for index in range(6000):
        cid, cluster = f"c{index}", f"cl-{index}"
        live[cid] = (cluster, "price")
        if len(live) > 30:  # the store keeps a small live set; older members/clusters leave
            live.pop(next(iter(live)))
        recorder.note_tick_skips("s", [("topic_cooldown", (cid,))])
        recorder.sync_membership(
            "s", live, lambda _c: None, now=time.time(), horizon_sec=75.0, force=True
        )
    from backend.application.live_follow.recorder import COMMENT_HISTORY, QUEUE_MAX

    assert len(state.cluster_revision) <= 60
    assert len(state.tick_deferred) <= COMMENT_HISTORY
    assert len(state.comments) <= COMMENT_HISTORY
    assert len(state.queue) <= QUEUE_MAX and len(state.membership) <= 30


# ---------------------------------------------------------------- composition


async def _events(case):
    await case.d.coordinator.live_follow.flush(case.sid)
    return [e for e in case.events if e["type"].startswith("live_follow.")]


async def _enabled_case(case_factory, monkeypatch, **kwargs):
    monkeypatch.setenv(ENV, "1")
    case = await case_factory(source=MultiSource({"product-a": UNITS}), **kwargs)
    case.d.coordinator.live_follow.bind(
        case.sid,
        {
            "tenant_id": "tenant-1",
            "business_session_id": "business-1",
            "runtime_session_id": case.sid,
            "generation": "generation-1",
        },
    )
    return case


def _qa_turn(case, members, cluster="cl-1", question="Bao lâu giao hàng?"):
    turn = decision(case, generated=True)
    turn.product_id = "product-a"
    turn.cluster_member_ids = tuple(members)
    turn.cluster_members = (question,)
    turn.source_cluster_id = cluster
    turn.topic = "shipping"
    return turn


async def _speak_qa(case, turn):
    co = case.d.coordinator
    co.live_follow.turn_selected(case.sid, turn, case.d.director.get_session(case.sid))
    await prepare(case, turn)
    co._speech_queue[case.sid].clear()
    assert await co._maybe_speak(case.sid, turn)


@pytest.mark.asyncio
async def test_flag_off_emits_nothing_on_the_real_path(case_factory, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    case = await case_factory(source=MultiSource({"product-a": UNITS}))
    await open_session(case)
    await play_next(case)
    await asyncio.sleep(0)
    assert not [e for e in case.events if e["type"].startswith("live_follow.")]
    assert any(e["type"] == "coordinator.speak_finished" for e in case.events)
    response = await case.client.get(f"/api/v1/sessions/{case.sid}/live-follow")
    assert response.status_code == 200 and response.json() == {
        "schema_version": 1,
        "enabled": False,
    }


@pytest.mark.asyncio
async def test_ten_units_with_qa_in_the_middle_keep_the_cursor(case_factory, monkeypatch):
    case = await _enabled_case(case_factory, monkeypatch)
    await open_session(case)
    for _ in range(3):
        await play_next(case)
    cursor = case.d.director.get_session(case.sid).director.state.products[0].next_unit
    turn = _qa_turn(case, ["c1", "c2", "c3"])
    await _speak_qa(case, turn)
    state = case.d.director.get_session(case.sid).director.state
    assert state.products[0].next_unit == cursor  # Q&A never advances the script cursor
    events = await _events(case)
    done = next(
        e for e in events if e["type"].endswith("turn.completed") and e["turn_id"] == turn.turn_id
    )
    assert (done["response_kind"], done["coverage_scope"], done["member_count"]) == (
        "answer",
        "cluster",
        3,
    )
    answering = [
        e["position"]
        for e in events
        if e["type"].endswith("position.updated") and e["position"]["phase"] == "answering_comment"
    ]
    assert answering and answering[0]["script_cursor"]["next_unit_index"] == cursor
    assert answering[0]["topic_code"] == "shipping" and answering[0]["member_count"] == 3
    await play_next(case)
    final = (await _events(case))[-1]
    assert final["type"].endswith("position.updated")
    assert final["position"]["script_cursor"]["next_unit_index"] == cursor + 1
    sequences = [e["follow_sequence"] for e in await _events(case)]
    assert sequences == sorted(sequences) and len(set(sequences)) == len(sequences)


@pytest.mark.asyncio
async def test_script_unit_position_names_version_and_unit(case_factory, monkeypatch):
    case = await _enabled_case(case_factory, monkeypatch)
    await open_session(case)
    await play_next(case)
    events = await _events(case)
    speaking = [
        e["position"]
        for e in events
        if e["type"].endswith("position.updated") and e["position"]["phase"] == "speaking"
    ][-1]
    assert speaking["unit_index"] == 1 and speaking["unit_count"] == len(UNITS)
    assert speaking["segment"]["approved_version_id"] == case.source.versions["product-a"].id
    assert speaking["unit_text_sha256"] and speaking["part_id"] is None
    assert speaking["media_started"] in (True, False)


@pytest.mark.asyncio
async def test_deflection_is_reported_as_deflection(case_factory, monkeypatch):
    case = await _enabled_case(case_factory, monkeypatch)

    def unavailable(*args, **kwargs):
        raise ConnectionError("provider unavailable")

    case.llm.stream_chunks = unavailable
    turn = _qa_turn(case, ["c1", "c2"], question="Sản phẩm này bảo hành mấy năm?")
    await _speak_qa(case, turn)
    done = next(e for e in await _events(case) if e["type"].endswith("turn.completed"))
    assert done["response_kind"] == "deflection"


@pytest.mark.asyncio
async def test_failed_speech_reports_turn_failed_and_members_wait(case_factory, monkeypatch):
    case = await _enabled_case(case_factory, monkeypatch)
    case.d.coordinator.live_follow.sync_membership(
        case.sid,
        {"c1": ("cl-1", "shipping"), "c2": ("cl-1", "shipping")},
        lambda cid: None,
        now=time.time(),
        horizon_sec=75.0,
        force=True,
    )
    turn = _qa_turn(case, ["c1", "c2"])
    co = case.d.coordinator
    co.live_follow.turn_selected(case.sid, turn, case.d.director.get_session(case.sid))
    await prepare(case, turn)
    co._speech_queue[case.sid].clear()

    def boom(*args, **kwargs):
        raise ConnectionError("tts down")
        yield

    case.tts.stream_audio = boom
    await co._maybe_speak(case.sid, turn)
    events = await _events(case)
    failed = next(e for e in events if e["type"].endswith("turn.failed"))
    assert failed["turn_id"] == turn.turn_id and failed["reason_code"] in {
        "failed",
        "playback_timeout",
    }
    assert "tts down" not in json.dumps(events)
    states = {c["state"] for c in co.live_follow.snapshot(case.sid)["comments"]}
    assert states == {"deferred"}


@pytest.mark.asyncio
async def test_interrupt_reports_cancelled_turn(case_factory, monkeypatch):
    case = await _enabled_case(case_factory, monkeypatch)
    co = case.d.coordinator
    turn = _qa_turn(case, ["c1"])
    co.live_follow.turn_selected(case.sid, turn, case.d.director.get_session(case.sid))
    co._speech_queue[case.sid].append(turn)
    await co.interrupt(case.sid)
    cancelled = next(e for e in await _events(case) if e["type"].endswith("turn.cancelled"))
    assert (cancelled["turn_id"], cancelled["reason_code"]) == (turn.turn_id, "interrupt")


@pytest.mark.asyncio
async def test_reducer_membership_events_and_snapshot_endpoint(case_factory, monkeypatch):
    case = await _enabled_case(case_factory, monkeypatch)
    co, reducer, sid = case.d.coordinator, case.d.reducer, case.sid
    co.mark_reducer_ready(sid)
    for index in range(3):
        reducer.notify_new_events(
            sid,
            comment=AcceptedComment(
                event_id=f"evt-{index}",
                comment_id=f"c{index}",
                text="giá bao nhiêu vậy shop",
                ts=time.time(),
                viewer_key=f"v{index}",
                provenance={
                    "platform": "facebook",
                    "connected_account_id": "acc",
                    "external_session_id": "live",
                    "source_message_id": f"m{index}",
                },
            ),
        )
    await reducer.run_once(sid, time.time())
    events = await _events(case)
    changes = [c for e in events if e["type"].endswith("cluster_assigned") for c in e["changes"]]
    assert {c["comment_id"] for c in changes} == {"c0", "c1", "c2"}
    assert len({c["cluster_id"] for c in changes}) == 1
    assert all(c["comment_key"].startswith("facebook/acc/live/m") for c in changes)
    response = await case.client.get(f"/api/v1/sessions/{sid}/live-follow")
    snapshot = response.json()
    assert response.status_code == 200 and snapshot["enabled"] is True
    assert snapshot["tenant_id"] == "tenant-1" and snapshot["generation"]
    assert snapshot["watermark"] == max(e["follow_sequence"] for e in events)
    assert {c["comment_id"] for c in snapshot["comments"]} == {"c0", "c1", "c2"}
    unknown = await case.client.get("/api/v1/sessions/nope/live-follow")
    assert unknown.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("occurred_at_out_of_range", "expired"),
        ("replay_flood", "duplicate_content"),
        ("profanity", "safety_filtered"),
    ],
)
async def test_intake_rejections_map_to_normalized_reasons(reason, code):
    from backend.application.platform_events.ingestion import PlatformEventIngestionService

    recorder, sent = _recorder()
    service = object.__new__(PlatformEventIngestionService)
    service._coordinator = SimpleNamespace(live_follow=recorder, has=lambda _sid: True)
    service._live_follow_rejected(
        "s", SimpleNamespace(type="viewer.comment", event_id="e1"), reason
    )
    service._live_follow_rejected("s", SimpleNamespace(type="viewer.signal", event_id="e2"), reason)
    events = await _drain(recorder, sent)
    assert [(e["event_ids"], e["reason_code"], e["final"]) for e in events] == [
        (["e1"], code, True)
    ]


def test_only_the_fixed_texts_count_as_deflection():
    from backend.application.script_authoring.answer_context import deflection_for, is_deflection

    assert is_deflection(deflection_for("bảo hành mấy năm", []))
    assert is_deflection(deflection_for("bitcoin giá bao nhiêu", []))
    assert not is_deflection(CLAIM)
