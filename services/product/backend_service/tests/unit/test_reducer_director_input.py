"""P0-FB-014 — the bounded reducer is the only viewer-demand input for P0 sessions.

Each test carries the negative-test-map row id it proves (brief §"Required
negative-test map", 16 rows). The map is the acceptance checklist.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import pytest

from backend.api.v1 import ProductEntityIn
from backend.api.v1.router import build_run_plan
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.director import coordinator as coord_mod
from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
from backend.application.director.decision import Decision, Director
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.session_context import DirectorRuntime
from backend.application.director.state import Phase, ProductStatus
from backend.application.platform_events import PlatformEvent
from backend.application.platform_events.ingestion import PlatformEventIngestionService
from backend.application.platform_events.models import MAX_STALENESS_SEC
from backend.application.reducer import (
    AcceptedComment,
    FastReducer,
    FastReducerConfig,
)
from backend.application.render.locks import SessionLockRegistry
from backend.application.render.orchestrator import StreamingControllerConfig
from backend.application.render.windows import AudioWindow
from backend.application.safety_gate import SafetyGate
from backend.application.text_chunker import FixedChunkPolicyConfig, TextChunk
from avatar.engines.mock import MockRenderBackend, _MockSession
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from tts.engines.base import TTSEngine, TTSRequest

pytestmark = pytest.mark.asyncio


# ── stubs ───────────────────────────────────────────────────────────


class _StubLLM(LLMEngine):
    name = "stub-llm"

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubLLM":
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:
        raise RuntimeError("stub")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Any:
        yield TextChunk(
            session_id=session_id,
            utterance_id=utterance_id,
            seq=0,
            text="Cau tra loi.",
            is_final=True,
        )


class _StubTTS(TTSEngine):
    name = "stub-tts"
    sample_rate = 24000

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubTTS":
        return cls()

    def synthesize(self, req: TTSRequest) -> Any:
        raise RuntimeError("stub")

    def stream_audio(
        self,
        text_or_chunk,
        *,
        session_id="",
        utterance_id="",
        req=None,
        min_ms=500,
        target_ms=1000,
        max_ms=2000,
    ) -> Any:
        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else str(text_or_chunk)
        sid = text_or_chunk.session_id if isinstance(text_or_chunk, TextChunk) else session_id
        yield AudioWindow(
            session_id=sid,
            utterance_id=utterance_id,
            seq=0,
            sample_rate=24000,
            duration_ms=100,
            pcm=b"\x01\x00" * 2400,
            is_final=True,
            text_span=text,
        )


def _products() -> list:
    return [
        ProductEntityIn(
            id="P001",
            name="Kem chong nang SPF50",
            description="Kem chong nang SPF50",
            price=350000,
        ).to_entity(),
        ProductEntityIn(
            id="P002",
            name="Serum Vitamin C",
            description="Serum Vitamin C lam sang da",
            price=250000,
        ).to_entity(),
    ]


def _register(backend: MockRenderBackend, session_id: str) -> None:
    sess = _MockSession(session_id=session_id)
    sess.idle_loop_frames = backend._build_idle_loop(session_id)
    backend._sessions[session_id] = sess


def _coordinator(session_id: str, reducer=None) -> tuple:
    backend = MockRenderBackend()
    _register(backend, session_id)
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coord = DirectorCoordinator(
        runtime=runtime,
        llm=_StubLLM(),
        tts=_StubTTS(),
        backend=backend,
        fixed_config=FixedChunkPolicyConfig(),
        controller_config=StreamingControllerConfig(),
        lock_registry=SessionLockRegistry(),
        cfg=CoordinatorConfig(tick_ms=50),
        reducer=reducer,
    )
    return coord, runtime, backend


def _reducer(**cfg) -> FastReducer:
    return FastReducer(config=FastReducerConfig(**cfg), embedder=HashingEmbedder())


async def _seed(reducer: FastReducer, session_id: str, comments: list[dict], *, now: float) -> None:
    for c in comments:
        reducer.notify_new_events(
            session_id,
            comment=AcceptedComment(
                event_id=f"ev-{c['comment_id']}",
                comment_id=c["comment_id"],
                text=c["text"],
                ts=c["ts"],
                viewer_key=c.get("viewer_key"),
                provenance={"event_id": f"ev-{c['comment_id']}", "occurred_at": c["ts"]},
            ),
        )
    await reducer.run_once(session_id, now)


def _comment(cid: str, text: str, ts: float, viewer: str) -> dict:
    return {"comment_id": cid, "text": text, "ts": ts, "viewer_key": viewer}


def _p0_event(
    event_id: str,
    text: str,
    *,
    occurred_at: Optional[float] = None,
    viewer_id: str = "v1",
) -> PlatformEvent:
    return PlatformEvent(
        event_id=event_id,
        platform="facebook",
        source_stream_id="bs-1",
        contract_version="p0.v1",
        tenant_id="t1",
        business_session_id="bs-1",
        connected_account_id="ca-1",
        external_session_id="ext-1",
        source_message_id=f"sm-{event_id}",
        moderation_ref="mod-1",
        occurred_at=occurred_at if occurred_at is not None else time.time(),
        type="viewer.comment",
        viewer={"viewer_id": viewer_id},
        payload={"text": text},
    )


def _p0_meta() -> dict:
    return {
        "status": "active",
        "execution_contract": {
            "tenant_id": "t1",
            "business_session_id": "bs-1",
            "runtime_session_id": "s1",
            "generation": 1,
        },
        "platform_event_binding": {
            "contract_version": "p0.v1",
            "tenant_id": "t1",
            "business_session_id": "bs-1",
            "platform": "facebook",
            "connected_account_id": "ca-1",
            "external_session_id": "ext-1",
        },
    }


def _selling_director(*, cur_index: int = 0, stage_turn_index: int = 0) -> Director:
    """A Director already past the protected opening, mid-stage on a product."""
    ents = _products()
    runtime = DirectorRuntime(backend=MockRenderBackend(), embedder=HashingEmbedder())
    runtime.attach("s1", ents, run_plan=build_run_plan(ents))
    state = runtime.get_session("s1").director.state
    state.phase = Phase.SELLING
    state.cursor.phase = "selling"
    state.cursor.opening_completed = True
    state.cursor.opening_turn_index = 3
    state.current_product_index = cur_index
    cur = state.current_product()
    cur.status = ProductStatus.ACTIVE
    cur.is_introduced = True
    cur.stage_turn_index = stage_turn_index
    return runtime.get_session("s1").director


# ══════════════════════════════════════════════════════════════════════
# R1 — one comment, one decision path
# ══════════════════════════════════════════════════════════════════════


async def test_r01_p0_session_reaches_the_director_only_through_the_reducer() -> None:
    """R1: one routed comment, consumed once, via reducer output only."""
    reducer = _reducer()
    coord, runtime, _ = _coordinator("s1", reducer=reducer)
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)
    coord.ingest("s1", "giá bao nhiêu", "v1", ts=time.time())

    calls: list[int] = []
    original = coord_mod.cluster_comments

    def _spy(window, merge_threshold=0.55):
        calls.append(len(window))
        return original(window, merge_threshold=merge_threshold)

    coord_mod.cluster_comments = _spy
    try:
        await coord._tick_once("s1")
    finally:
        coord_mod.cluster_comments = original

    state = runtime.get_session("s1").director.state
    # The raw decision feed is off: rolling_comments stays empty and the
    # legacy clustering path never ran for this session.
    assert state.rolling_comments == []
    assert calls == []
    coord.stop("s1")


async def test_r01_reducer_mode_without_a_reducer_refuses_instead_of_falling_back() -> None:
    """R1: reducer mode with no store decides nothing — no silent legacy fallback."""
    from backend.application.director.reducer_input import build_selections

    director = _selling_director()
    assert (
        build_selections(
            director, store=None, reducer_now=10.0, director_now=10.0, wall_now=time.time()
        )
        == []
    )
    # And the legacy path is not silently substituted either.
    assert director.decide([], now=10.0).action != "answer_cluster"


# ══════════════════════════════════════════════════════════════════════
# R2 — legacy and reducer feeds concurrently
# ══════════════════════════════════════════════════════════════════════


async def test_r02_legacy_session_keeps_its_raw_decision_path() -> None:
    """R2: a session that never opted in is unchanged: raw feed still works."""
    coord, runtime, _ = _coordinator("legacy")
    coord.start("legacy", _products(), activated=True)
    for i in range(4):
        coord.ingest("legacy", f"giá bao nhiêu {i}", f"u{i}", ts=time.time())

    await coord._tick_once("legacy")
    state = runtime.get_session("legacy").director.state
    assert len(state.rolling_comments) == 4
    assert state.rolling_comments[0].intent == "price"
    coord.stop("legacy")


async def test_r02_no_execution_runs_both_feeds() -> None:
    """R2: reducer mode is per-session; one process, two sessions, two feeds."""
    reducer = _reducer()
    coord, runtime, backend = _coordinator("p0", reducer=reducer)
    _register(backend, "legacy")
    coord.start("p0", _products(), activated=True)
    coord.set_reducer_mode("p0", True)
    coord.start("legacy", _products(), activated=True)

    coord.ingest("p0", "giá bao nhiêu", "a", ts=time.time())
    coord.ingest("legacy", "giá bao nhiêu", "b", ts=time.time())
    await coord._tick_once("p0")
    await coord._tick_once("legacy")

    assert runtime.get_session("p0").director.state.rolling_comments == []
    assert len(runtime.get_session("legacy").director.state.rolling_comments) == 1
    assert coord.reducer_mode("p0") is True
    assert coord.reducer_mode("legacy") is False
    coord.stop("p0")
    coord.stop("legacy")


# ══════════════════════════════════════════════════════════════════════
# R3 — bounded burst
# ══════════════════════════════════════════════════════════════════════


async def test_r03_burst_keeps_prompt_context_at_max_representatives() -> None:
    """R3: a burst over every bound still yields <= max_representatives questions."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer(
        max_pending=10, max_active_clusters=2, max_members_per_cluster=5, max_representatives=3
    )
    now = time.time()
    burst = [
        _comment(f"c{i}", f"gia bao nhieu bao lao cau {i}", now - i * 0.01, f"v{i}")
        for i in range(200)
    ]
    await _seed(reducer, "s1", burst, now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    assert selections, "burst must still produce a decision candidate"
    for selection in selections:
        assert len(selection.envelope.representative_questions) <= store.config.max_representatives
        assert len(selection.cluster_members) <= store.config.max_representatives
    assert store.cluster_count() <= 2
    for cluster in store.active_clusters(now):
        assert len(cluster.member_ids) <= 5


async def test_r03_no_raw_member_transcript_reaches_the_prompt() -> None:
    """R3: only representative questions are prompt context, never the transcript."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer(max_representatives=2)
    now = time.time()
    comments = [_comment(f"c{i}", f"gia bao nhieu {i}", now, f"v{i}") for i in range(6)]
    await _seed(reducer, "s1", comments, now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    assert selections
    for selection in selections:
        assert len(selection.cluster_members) <= store.config.max_representatives

    # Prove it on the REAL prompt builders, not just on the adapter output: one
    # member carries a marker text and the prompts must not contain it unless
    # that member happens to be a representative. Both prompts are exercised.
    from backend.application.director.reducer_input import select_scored

    scored = select_scored(director, selections, now=now)
    assert scored
    top = scored[0]
    representative = set(top.cluster.members)
    non_representative = [
        text
        for cid, text in store.get_cluster(selections[0].envelope.cluster_id)._member_texts.items()
        if text not in representative
    ]
    assert non_representative, "the cluster must have members outside the representative cap"
    forbidden = non_representative[0]
    for prompt in (
        director._answer_prompt(top),
        director._grounded_prompt(top, "P001", "commerce.price.current", "350000"),
    ):
        assert forbidden not in prompt
        # And the cap holds inside the prompt itself.
        assert prompt.count(" | ") <= store.config.max_representatives - 1


# ══════════════════════════════════════════════════════════════════════
# R4 — minority high-value question during selling
# ══════════════════════════════════════════════════════════════════════


async def test_r04_singleton_high_value_beats_larger_low_value_before_stage_two() -> None:
    """R4: a lone high-value cluster is selected with the Q&A window closed."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    now = time.time()
    comments = [_comment(f"lo{i}", "cách dùng sản phẩm thế nào", now, f"lo{i}") for i in range(5)]
    comments.append(_comment("hi1", "shop giao sai hàng", now, "hi1"))
    await _seed(reducer, "s1", comments, now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    cur = director.state.current_product()
    # Q&A window shut: stage index below 2, window not open.
    assert cur.stage_turn_index < 2
    assert director.state.qa_window_open is False

    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    high = [s for s in selections if director.is_high_value(s.envelope)]
    assert high, "the singleton high-value cluster must be selectable"

    decision = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: True)
    assert decision.action in ("answer_cluster", "answer_fact")
    # Checkpoint / no-nested-pivot rules still hold.
    assert director.state.cursor.pivot_active is False
    assert decision.queued_pivot_products == ()


async def test_r04_low_value_singleton_is_still_dropped() -> None:
    """R4: relaxing the stage gates is scoped to high-value clusters only."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    now = time.time()
    await _seed(reducer, "s1", [_comment("lo1", "cách dùng sản phẩm thế nào", now, "lo1")], now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    decision = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: False)
    assert decision.action not in ("answer_cluster", "answer_fact")


# ══════════════════════════════════════════════════════════════════════
# R5 — stale relevant vs hard-expired
# ══════════════════════════════════════════════════════════════════════


async def test_r05_delayed_relevant_question_keeps_its_original_occurred_at() -> None:
    """R5: inside the horizon it stays eligible and its age is never reset."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer(rolling_horizon_sec=75.0)
    now = time.time()
    old = now - 60.0
    await _seed(reducer, "s1", [_comment("c1", "gia bao nhieu", old, "v1")], now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    assert selections
    selection = selections[0]
    assert selection.oldest_occurred_at == pytest.approx(old)
    assert selection.newest_occurred_at == pytest.approx(old)
    # Director-clock conversion is the same instant, not a reset one.
    assert selection.cluster_newest_t == pytest.approx(old)


async def test_r05_hard_expired_event_is_rejected_at_ingress_never_selected() -> None:
    """R5: >24h old is rejected at ingress (013) and never reaches a decision."""
    store = InMemorySessionStore()
    await store.set("s1", _p0_meta())
    reducer = _reducer()
    service = PlatformEventIngestionService(store=store, reducer=reducer)
    stale = _p0_event("old-1", "gia bao nhieu", occurred_at=time.time() - MAX_STALENESS_SEC - 60)
    result = await service.ingest("s1", [stale], delivery_outcomes_v1=True)
    assert result["rejected"] == 1
    assert result["events"][0]["reason"] == "occurred_at_out_of_range"
    assert reducer.pending_count("s1") == 0


# ══════════════════════════════════════════════════════════════════════
# R6 — duplicate cluster / duplicate delivery
# ══════════════════════════════════════════════════════════════════════


async def test_r06_duplicate_event_never_creates_a_second_member_or_decision() -> None:
    """R6: a retried event id is a duplicate and adds no second member."""
    store = InMemorySessionStore()
    await store.set("s1", _p0_meta())
    reducer = _reducer()
    service = PlatformEventIngestionService(store=store, reducer=reducer)
    event = _p0_event("dup-1", "gia bao nhieu")
    first = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert first["events"][0]["status"] == "not_ready"
    # A retryable outcome consumes no dedup identity (I-2), so the retry is
    # re-evaluated rather than silently swallowed as a duplicate.
    retry = await service.ingest("s1", [event], delivery_outcomes_v1=True)
    assert retry["events"][0]["status"] == "not_ready"
    assert retry["events"][0]["action_identity"] == first["events"][0]["action_identity"]
    assert reducer.pending_count("s1") == 0

    # A ROUTED delivery DOES consume the identity: the retry is a duplicate and
    # creates no second member.
    coord, _runtime, _backend = _coordinator("s1", reducer=reducer)
    routed_service = PlatformEventIngestionService(store=store, coordinator=coord, reducer=reducer)
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)
    routed = _p0_event("dup-2", "gia bao nhieu")
    assert (await routed_service.ingest("s1", [routed], delivery_outcomes_v1=True))["events"][0][
        "status"
    ] == "routed"
    again = await routed_service.ingest("s1", [routed], delivery_outcomes_v1=True)
    assert again["duplicate"] == 1
    assert again["events"][0].get("comment_id") is None
    coord.stop("s1")


async def test_r06_answered_cluster_is_not_re_answered_without_novelty() -> None:
    """R6: mark_answered + the existing signature/cooldown suppress a repeat."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    now = time.time()
    comments = [_comment(f"c{i}", f"gia bao nhieu {i}", now, f"v{i}") for i in range(3)]
    await _seed(reducer, "s1", comments, now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    assert selections
    selection = selections[0]
    store.mark_answered(selection.envelope.cluster_id, now)

    # Answer it once, then decide again with the same members.
    first = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: False)
    director.mark_spoken(first)
    first.completed_at = now
    director.mark_spoken(first)
    second = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: False)
    assert second.action != first.action or second.task_id != first.task_id


# ══════════════════════════════════════════════════════════════════════
# R7 — unknown fact
# ══════════════════════════════════════════════════════════════════════


async def test_r07_unknown_fact_yields_no_unsupported_claim() -> None:
    """R7: no approved fact -> answer_cluster through the 005 validator, never a bare claim."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    now = time.time()
    await _seed(reducer, "s1", [_comment("c1", "thuong hieu nay la gi", now, "v1")], now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    decision = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: True)
    assert decision.action == "answer_cluster"
    assert decision.text is None
    assert decision.prompt


# ══════════════════════════════════════════════════════════════════════
# R8 — local artifact resume
# ══════════════════════════════════════════════════════════════════════


async def test_r08_resume_product_returns_to_checkpoint_and_keeps_answered_state() -> None:
    """R8: after an excursion, resume restores the checkpoint without replaying answers."""
    director = _selling_director()
    state = director.state
    state.goto_product("P002")
    state.cursor.checkpoint_product_id = "P001"
    state.cursor.checkpoint_stage = "benefit"
    state.cursor.checkpoint_turn_index = 2
    state.products[0].stage_turn_index = 2
    state.qa_last_comment_signature["P001:price"] = "gia bao nhieu"
    state.answered_comments.add("c-old")

    decision = Decision(
        action="resume_product",
        product_id="P001",
        stage="resume",
        task_id="P001:resume",
        resume_product_id="P001",
        reason="pivot lifecycle completed",
    )
    director.mark_spoken(decision)

    cur = state.current_product()
    assert cur.product_id == "P001"
    assert cur.stage_turn_index == 2
    assert state.cursor.pivot_active is False
    # Already-answered content is still suppressed after the excursion.
    assert state.qa_last_comment_signature["P001:price"] == "gia bao nhieu"
    assert "c-old" in state.answered_comments


# ══════════════════════════════════════════════════════════════════════
# R9 — multiple approved products
# ══════════════════════════════════════════════════════════════════════


async def test_r09_product_candidates_resolve_against_this_session_catalog() -> None:
    """R9: resolution uses the session catalog, not a process-global one."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    reducer.set_session_catalog("s1", _products(), "P001")
    now = time.time()
    await _seed(
        reducer, "s1", [_comment("c1", "serum vitamin c gia bao nhieu", now, "v1")], now=now
    )
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    assert selections
    assert "P002" in selections[0].resolved_product_ids


async def test_r09_coverage_read_skips_a_covered_stage_and_progression_advances() -> None:
    """R9: a fully covered stage is skipped, and progression still reaches the next product."""
    from backend.application.director.scoring import coverage_ratio

    director = _selling_director()
    state = director.state
    phase = state.run_plan.selling[0]
    assert phase.key_selling_points
    state.mark_product_covered("P001", set(phase.key_selling_points))
    assert coverage_ratio(state.covered_points.get("P001"), phase.key_selling_points) == 1.0

    cur = state.current_product()
    # The next sales turn skips the covered stage instead of repeating it.
    assert director._next_sales_turn(cur) is None
    cur.stage_turn_index = len(director._sales_tasks("P001"))
    director._advance_product()
    assert state.current_product().product_id == "P002"


# ══════════════════════════════════════════════════════════════════════
# R10 — teardown after route, before consumption
# ══════════════════════════════════════════════════════════════════════


async def test_r10_teardown_after_route_never_reaches_the_reducer() -> None:
    """R10: reconciled non_deliverable; the reducer never saw the comment."""
    reducer = _reducer()
    coord, runtime, _ = _coordinator("s1", reducer=reducer)
    store = InMemorySessionStore()
    await store.set("s1", _p0_meta())
    service = PlatformEventIngestionService(store=store, coordinator=coord, reducer=reducer)
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)

    result = await service.ingest(
        "s1", [_p0_event("td-1", "gia bao nhieu")], delivery_outcomes_v1=True
    )
    assert result["events"][0]["status"] == "routed"
    # Route-time notification is off in reducer mode: the reducer is fed at
    # consumption only, so a teardown before consumption leaves it untouched.
    assert reducer.pending_count("s1") == 0

    attach_seq = coord.stop("s1")
    reconciled = await service.reconcile_session("s1", attach_seq=attach_seq)
    assert reconciled == ["td-1"]
    assert service.terminal_outcomes("s1")["td-1"].outcome == "non_deliverable"
    assert reducer.pending_count("s1") == 0
    assert not reducer.session_active("s1")


# ══════════════════════════════════════════════════════════════════════
# R11 — reattach / new generation
# ══════════════════════════════════════════════════════════════════════


async def test_r11_stop_drops_reducer_state_so_reattach_inherits_nothing() -> None:
    """R11: no cluster, answered state or provenance survives stop + start."""
    reducer = _reducer()
    now = time.time()
    await _seed(reducer, "s1", [_comment("c1", "gia bao nhieu", now, "v1")], now=now)
    assert reducer.session_active("s1")

    coord, runtime, _ = _coordinator("s1", reducer=reducer)
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)
    coord.stop("s1")

    assert not reducer.session_active("s1")
    assert reducer.pending_count("s1") == 0
    assert reducer.provenance_for("s1", "ev-c1") is None

    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)
    # Reducer mode refuses while there is no store: decide nothing, never fall
    # back to the legacy feed.
    assert coord._reducer_store("s1") is None
    fresh = reducer.session_store("s1")
    assert fresh is None or fresh.active_clusters(time.time()) == []
    coord.stop("s1")


# ══════════════════════════════════════════════════════════════════════
# R12 — two concurrent sessions
# ══════════════════════════════════════════════════════════════════════


async def test_r12_catalogs_clusters_and_fences_are_isolated_per_session() -> None:
    """R12: two sessions in one process never share catalog, cluster or fence."""
    reducer = _reducer()
    coord, runtime, backend = _coordinator("a", reducer=reducer)
    _register(backend, "b")
    coord.start("a", _products(), activated=True)
    coord.start("b", _products(), activated=True)
    coord.set_reducer_mode("a", True)
    coord.set_reducer_mode("b", True)
    reducer.set_session_catalog("a", _products(), "P001")
    reducer.set_session_catalog("b", _products(), "P002")

    now = time.time()
    await _seed(reducer, "a", [_comment("a1", "kem chong nang gia bao nhieu", now, "va")], now=now)
    await _seed(reducer, "b", [_comment("b1", "serum vitamin c gia bao nhieu", now, "vb")], now=now)
    assert reducer.session_catalog("a")[1] == "P001"
    assert reducer.session_catalog("b")[1] == "P002"
    assert [c.member_ids for c in reducer.session_store("a").active_clusters(now)] == [["a1"]]
    assert [c.member_ids for c in reducer.session_store("b").active_clusters(now)] == [["b1"]]

    # Per-session delivery fences via the real coordinator counter (P-F3).
    coord.ingest("a", "x", "va", ts=now)
    coord.ingest("a", "y", "va", ts=now)
    coord.ingest("b", "z", "vb", ts=now)
    assert coord.next_delivery_tick("a") == 2
    assert coord.next_delivery_tick("b") == 1
    assert coord.stop("a") == 2
    assert coord.next_delivery_tick("b") == 1
    coord.stop("b")


# ══════════════════════════════════════════════════════════════════════
# R13 — retryable not_ready / queue_full
# ══════════════════════════════════════════════════════════════════════


async def test_r13_not_ready_never_notifies_the_reducer_or_decides() -> None:
    """R13: retryable outcomes keep their identity and never reach the reducer."""
    store = InMemorySessionStore()
    await store.set("s1", _p0_meta())
    reducer = _reducer()
    service = PlatformEventIngestionService(store=store, reducer=reducer)
    result = await service.ingest(
        "s1", [_p0_event("nr-1", "gia bao nhieu")], delivery_outcomes_v1=True
    )
    assert result["events"][0]["status"] == "not_ready"
    assert result["events"][0]["reason"] == "no_coordinator_attached"
    assert reducer.pending_count("s1") == 0
    assert service.terminal_outcomes("s1") == {}


async def test_r13_p0_session_without_the_opt_in_keeps_the_route_time_notification() -> None:
    """R13: reducer mode alone must not defer — the opt-in has to actually fire.

    A P0 session whose caller never sets ``delivery_outcomes_v1`` is still on
    the legacy contract, so it keeps today's route-time reducer notification.
    Deferring on mode alone would silently starve the reducer of that traffic.
    """
    reducer = _reducer()
    coord, _runtime, _backend = _coordinator("s1", reducer=reducer)
    store = InMemorySessionStore()
    await store.set("s1", _p0_meta())
    service = PlatformEventIngestionService(store=store, coordinator=coord, reducer=reducer)
    coord.comment_consumed = service.mark_consumed
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)

    legacy = await service.ingest("s1", [_p0_event("leg-1", "giá bao nhiêu")])
    assert legacy["events"][0]["status"] == "accepted"
    # No ``reason`` on purpose. ``reason``/``action_identity`` ride the truthful
    # opt-in only (ingestion.py:619-623, 013 I-2), precisely so the deployed
    # API's response shape is byte-for-byte unchanged while the flag is off.
    # R13 ingests without the opt-in, so their absence IS the contract.
    assert "reason" not in legacy["events"][0]
    # Not deferred: the reducer already holds it, route-time, as before.
    assert reducer.pending_count("s1") == 1
    # And reducer mode still refuses to decide, because readiness never fired.
    assert coord._reducer_store("s1") is None
    coord.stop("s1")


# ══════════════════════════════════════════════════════════════════════
# R14 — provenance through decision
# ══════════════════════════════════════════════════════════════════════


async def test_r14_provenance_travels_beside_the_envelope_never_inside_it() -> None:
    """R14: event ids + occurred_at ride beside; the envelope stays id-free."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    coord, runtime, _ = _coordinator("s1", reducer=reducer)
    store = InMemorySessionStore()
    await store.set("s1", _p0_meta())
    service = PlatformEventIngestionService(store=store, coordinator=coord, reducer=reducer)
    # The composition root's one sink, wired the same way (I-4).
    coord.comment_consumed = service.mark_consumed
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)

    occurred = time.time() - 5
    routed = await service.ingest(
        "s1",
        [_p0_event("pv-1", "giá bao nhiêu", occurred_at=occurred)],
        delivery_outcomes_v1=True,
    )
    assert routed["events"][0]["status"] == "routed"
    comment_id = routed["events"][0]["comment_id"]

    # The reducer is notified at CONSUMPTION, not at route time.
    assert reducer.pending_count("s1") == 0
    await coord._tick_once("s1")
    assert reducer.pending_count("s1") == 1
    now = time.time()
    await reducer.run_once("s1", now)

    provenance = reducer.provenance_for("s1", comment_id)
    assert provenance is not None
    assert provenance["event_id"] == "pv-1"
    assert provenance["tenant_id"] == "t1"
    assert provenance["platform"] == "facebook"
    assert provenance["occurred_at"] == pytest.approx(occurred)

    director = _selling_director()
    selections = build_selections(
        director,
        store=reducer.session_store("s1"),
        reducer_now=now,
        director_now=now,
        wall_now=now,
        provenance=lambda cid: reducer.provenance_for("s1", cid),
    )
    assert selections
    selection = selections[0]
    assert selection.member_comment_ids == (comment_id,)
    assert selection.source_event_ids == ("pv-1",)
    assert selection.oldest_occurred_at == pytest.approx(occurred)
    # The envelope itself carries no member or viewer ids.
    flat = repr(selection.envelope)
    assert "pv-1" not in flat
    assert not hasattr(selection.envelope, "member_ids")
    coord.stop("s1")


async def test_r14_decision_event_and_persisted_decision_carry_provenance() -> None:
    """R14: director.decision + the persisted row carry cluster id, event ids, bounds."""
    from backend.application.director.events import decision_to_event
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    now = time.time()
    await _seed(reducer, "s1", [_comment("c1", "gia bao nhieu", now, "v1")], now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    decision = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: True)
    assert decision.source_cluster_id == selections[0].envelope.cluster_id
    assert decision.source_event_ids == selections[0].source_event_ids
    assert decision.occurred_at_bounds == (
        selections[0].oldest_occurred_at,
        selections[0].newest_occurred_at,
    )

    event = decision_to_event(decision)
    assert event["cluster_id"] == decision.source_cluster_id
    assert list(event["source_event_ids"]) == list(decision.source_event_ids)
    assert event["oldest_occurred_at"] == decision.occurred_at_bounds[0]
    assert event["newest_occurred_at"] == decision.occurred_at_bounds[1]

    payload = decision.provenance_payload()
    assert payload["cluster_id"] == decision.source_cluster_id
    assert payload["source_event_ids"] == list(decision.source_event_ids)
    assert payload["oldest_occurred_at"] == decision.occurred_at_bounds[0]
    assert payload["newest_occurred_at"] == decision.occurred_at_bounds[1]


# ══════════════════════════════════════════════════════════════════════
# R15 — opening protected
# ══════════════════════════════════════════════════════════════════════


async def test_r15_reducer_selection_never_preempts_the_approved_opening() -> None:
    """R15: during the protected opening no reducer selection is eligible."""
    from backend.application.director.reducer_input import build_selections

    reducer = _reducer()
    now = time.time()
    await _seed(reducer, "s1", [_comment("c1", "ship co bao lao khong", now, "v1")], now=now)
    store = reducer.session_store("s1")
    director = _selling_director()
    state = director.state
    state.phase = Phase.OPENING
    state.cursor.phase = "opening"
    state.cursor.opening_completed = False
    state.cursor.opening_turn_index = 0

    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    assert selections
    decision = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: True)
    assert decision.action == "speak_hook"
    assert decision.stage == "opening"


# ══════════════════════════════════════════════════════════════════════
# R16 — SafetyGate / approved speech
# ══════════════════════════════════════════════════════════════════════


async def test_r16_reducer_driven_decision_uses_the_unchanged_approved_speech_boundary() -> None:
    """R16: every reducer-driven answer still goes through _prepare_approved."""
    from backend.application.director.reducer_input import build_selections

    coord, _runtime, _backend = _coordinator("s1", reducer=_reducer())
    reducer = _reducer()
    now = time.time()
    await _seed(reducer, "s1", [_comment("c1", "giá bao nhiêu", now, "v1")], now=now)
    director = _selling_director()
    selections = build_selections(
        director, store=reducer.session_store("s1"), reducer_now=now, director_now=now, wall_now=now
    )
    decision = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: True)
    assert decision.action in ("answer_cluster", "answer_fact")

    import inspect

    source = inspect.getsource(DirectorCoordinator._prepare_approved)
    assert "self.approved_speech.prepare(" in source
    assert 'route="director"' in source
    # 014 adds no alternate speech path: the only speech owner is still the
    # one installed by the composition root.
    assert coord.approved_speech is None  # installed by the composition root only
    assert inspect.iscoroutinefunction(DirectorCoordinator._maybe_speak)
    # 014 adds no alternate speech path: the only place a Decision reaches the
    # orchestrator is still _maybe_speak, and the pre-speech boundary it calls
    # is still _prepare_approved.
    speak_calls = [
        name
        for name in vars(DirectorCoordinator)
        if inspect.iscoroutinefunction(vars(DirectorCoordinator)[name])
    ]
    assert "_maybe_speak" in speak_calls
    assert "_prepare_approved" in speak_calls
    assert not any("speak" in name and name not in ("_maybe_speak",) for name in speak_calls)


async def test_r16_unsafe_comment_is_rejected_at_intake_before_any_decision() -> None:
    """R16: the SafetyGate runs at intake; a rejected comment never clusters."""
    store = InMemorySessionStore()
    await store.set("s1", _p0_meta())
    reducer = _reducer()
    service = PlatformEventIngestionService(store=store, reducer=reducer, safety_gate=SafetyGate())
    result = await service.ingest(
        "s1",
        [_p0_event("bad-1", "like and subscribe and share this video")],
        delivery_outcomes_v1=True,
    )
    assert result["rejected"] == 1
    assert reducer.pending_count("s1") == 0


# ══════════════════════════════════════════════════════════════════════
# Review fix — an empty reducer projection still reaches Director._decide
# ══════════════════════════════════════════════════════════════════════


def _ready_coordinator(reducer: FastReducer):
    coord, runtime, _ = _coordinator("s1", reducer=reducer)
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)
    coord.mark_reducer_ready("s1")
    return coord, runtime.get_session("s1")


async def test_empty_store_still_speaks_the_protected_opening_via_the_coordinator() -> None:
    import copy

    reducer = _reducer()
    coord, ds = _ready_coordinator(reducer)
    now = ds.now()
    store = reducer.session_store("s1")
    legacy = copy.deepcopy(ds.director).decide([], now)
    decision = coord._decide_from_reducer(ds.director, "s1", store, now)
    assert decision.action == legacy.action == "speak_hook"
    assert decision.stage == legacy.stage
    coord.stop("s1")


async def test_demand_lull_after_expiry_matches_legacy_decide() -> None:
    import copy

    reducer = _reducer()
    coord, ds = _ready_coordinator(reducer)
    wall = time.time()
    await _seed(reducer, "s1", [_comment("old", "giá bao nhiêu", wall - 400, "v1")], now=wall)
    store = reducer.session_store("s1")
    now = ds.now()
    legacy = copy.deepcopy(ds.director).decide([], now)
    decision = coord._decide_from_reducer(ds.director, "s1", store, now)
    assert decision.action == legacy.action
    assert decision.action != "idle"
    coord.stop("s1")


async def test_reducer_consumed_markers_are_pruned_past_the_drain_window() -> None:

    coord, _runtime, _ = _coordinator("s1", reducer=_reducer())

    class _C:
        def __init__(self, cid: str, ts: float) -> None:
            self.id, self.ts = cid, ts

    now = time.time()
    coord._remember_consumed("s1", [_C(f"old{i}", now - 10_000) for i in range(50)])
    coord._remember_consumed("s1", [_C("fresh", now)])
    assert set(coord._consumed_ids("s1")) == {"fresh"}


async def test_reducer_mode_tick_releases_real_chat_queue_capacity() -> None:
    """013 lifetime-capacity defect must not survive on the reducer path.

    Real coordinator + real ChatQueue in reducer mode: fill to ``max_size``,
    tick, capacity recovers, and a second full lap is accepted (no lifetime cap).
    """
    coord, _runtime, _ = _coordinator("s1", reducer=_reducer())
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)
    queue = coord._queues["s1"]
    size = queue.max_size

    for lap in range(2):
        for i in range(size):
            queue.put(f"q{lap}-{i}", f"v{i}", ts=time.time())
        assert queue.free_slots() == 0
        await coord._tick_once("s1")
        assert queue.free_slots() == size
        assert coord.queue_capacity("s1") == size
    coord.stop("s1")


async def test_held_reducer_session_still_consumes_but_freezes_director_timers() -> None:
    """Hold freezes timers and scheduling, never ingestion (013 + 014 + 016)."""
    from backend.application.script_authoring.approved_speech import ApprovedSpeech

    coord, runtime, _ = _coordinator("s1", reducer=_reducer())
    coord.approved_speech = ApprovedSpeech(None, lambda: None)
    coord.start("s1", _products(), activated=True)
    coord.set_reducer_mode("s1", True)
    state = runtime._sessions["s1"].director.state
    queue = coord._queues["s1"]
    coord.approved_speech.block("s1", "held")
    before = state.phase_elapsed_sec
    last = coord._last_tick["s1"]
    for i in range(3):
        queue.put(f"h{i}", f"v{i}", ts=time.time())
    await asyncio.sleep(0.06)  # clock must visibly advance on any platform
    await coord._tick_once("s1")
    assert queue.free_slots() == queue.max_size  # consumed, not dropped
    assert coord._last_tick["s1"] > last  # tick clock advanced
    assert state.phase_elapsed_sec == before  # timers frozen
    assert state.product_elapsed_sec == 0.0 and state.sec_since_relevant_msg == 0.0
    coord.stop("s1")


async def _noop_prepare(*_a, **_k) -> None:
    await asyncio.sleep(60)


async def test_hold_landing_while_waiting_for_decision_lock_prepares_nothing() -> None:
    from backend.application.script_authoring.approved_speech import ApprovedSpeech

    async def run(hold: bool) -> int:
        coord, _ds = _ready_coordinator(_reducer())
        coord.approved_speech = ApprovedSpeech(None, lambda: None)
        coord._prepare_turn = _noop_prepare  # preparation itself is not under test
        async with coord._decision_locks["s1"]:
            filling = asyncio.create_task(coord._fill_prepared("s1"))
            await asyncio.sleep(0.01)  # past the first _frozen check, parked on the lock
            if hold:
                coord.approved_speech.block("s1", "held")
        await filling
        n = len(coord._decision_queue["s1"]) + len(coord._prepare_tasks["s1"])
        coord.stop("s1")
        return n

    assert await run(hold=False) > 0  # control: the same setup does prepare
    assert await run(hold=True) == 0
