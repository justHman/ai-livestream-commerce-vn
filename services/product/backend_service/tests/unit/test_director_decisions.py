"""Regression checks for the Stage 2 auto-demo speech sequence."""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.engine_manager import EngineManager
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse

from .fixtures import MOCK_ENTITIES, MOCK_PRODUCTS
from backend.application.director.clustering import Comment
from backend.application.director.config import StreamConfig
from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
from backend.application.director.decision import Director
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.session_context import DirectorRuntime
from backend.application.director.state import Phase, ProductState, ProductStatus, StreamState
from backend.application.render.engines_base import FullPipelineBackend
from backend.application.render.locks import SessionLockRegistry
from avatar.engines.mock import MockRenderBackend, _MockSession


class _RecordingLLM(LLMEngine):
    name = "recording"

    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []

    @classmethod
    def from_config(cls, cfg):
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:
        self.requests.append(req)
        return LLMResponse(text="Câu trả lời hoàn chỉnh.")


class _CloudBackend(FullPipelineBackend):
    name = "test-cloud"

    def start(self, opts):
        raise NotImplementedError

    def interrupt(self, session_id: str) -> None:
        pass

    def stop(self, session_id: str) -> None:
        pass

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        return text


class _Agent:
    def __init__(self) -> None:
        self.wait_values: list[bool] = []

    def start_listening(self) -> None:
        pass

    def stop_listening(self) -> None:
        pass

    def stream_pcm(self, chunks, *, source_rate: int, wait: bool) -> None:
        list(chunks)
        self.wait_values.append(wait)


def test_engine_manager_forwards_cloud_generation_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    manager = EngineManager()
    manager._llm = _RecordingLLM()
    manager._llm_cfg = {"max_tokens": 8192, "temperature": 0.3}

    manager.get_llm_fn()("Giới thiệu sản phẩm")

    request = manager.llm.requests[0]
    assert (request.max_tokens, request.temperature) == (8192, 0.3)


def test_global_opening_has_three_grounded_turns_before_product_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    state = StreamState(
        phase=Phase.OPENING, products=[ProductState(product_id="P004", name="Áo hoodie")]
    )
    state.traffic.viewer_count = 100
    director = Director(state=state)

    turns = []
    for index in range(3):
        turn = director.decide([], now=float(index))
        turns.append(turn)
        director.mark_spoken(turn)

    assert [turn.action for turn in turns] == ["speak_hook", "speak_hook", "speak_hook"]
    assert state.cursor.opening_completed is True
    assert state.phase is Phase.SELLING


def test_product_sales_are_split_into_short_stage_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    product = MOCK_ENTITIES[0]
    state = StreamState(
        phase=Phase.SELLING,
        products=[ProductState(product_id=product.id, name=product.name)],
    )
    director = Director(state=state, catalog={product.id: product})

    intro = director.decide([], now=0.0)
    director.mark_spoken(intro)
    follow_up = director.decide([], now=1.0)

    assert intro.stage == "intro"
    assert "1 đến 2 câu" in intro.prompt
    assert follow_up.action == "sell_product"
    assert follow_up.stage in {"benefit", "offer", "trust", "cta"}
    assert "5 đến 7 câu hoàn chỉnh" not in intro.prompt


def test_demand_pivot_has_hysteresis_and_minimum_comment_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    from backend.application.director.pivot import should_enter_pivot, should_exit_pivot

    assert should_enter_pivot("P002", ["P002"] * 6 + ["P004"] * 4, top_score=0.9, current_score=0.7)
    assert not should_enter_pivot(
        "P002", ["P002"] * 3 + ["P004"] * 3, top_score=0.9, current_score=0.7
    )
    assert should_exit_pivot("P002", ["P002"] * 4 + ["P004"] * 6)
    assert not should_exit_pivot("P002", ["P002"] * 6 + ["P004"] * 4)


def _routed_comments(product_id: str, count: int, start: int = 0) -> list[Comment]:
    vector = [1.0, 0.0] if product_id == "P004" else [0.0, 1.0]
    return [
        Comment(
            id=f"{product_id}-{index}",
            text=f"{product_id} giá bao nhiêu {index}",
            embedding=vector,
            t=10.0 + index / 100,
            intent="price",
            product_id=product_id,
        )
        for index in range(start, start + count)
    ]


def _cross_product_director() -> Director:
    products = [
        ProductState(
            product_id="P004",
            name="Áo hoodie",
            is_introduced=True,
            stage="benefit",
            stage_turn_index=2,
        ),
        ProductState(product_id="P002", name="Serum"),
    ]
    from backend.api.v1.router import build_run_plan

    run_plan = build_run_plan(
        [
            {"id": "P004", "name": "Áo hoodie", "features": ["ấm và nhẹ"]},
            {"id": "P002", "name": "Serum", "features": ["dưỡng ẩm"]},
        ]
    )
    return Director(
        state=StreamState(
            phase=Phase.SELLING,
            products=products,
            run_plan=run_plan,
        ),
        cfg=StreamConfig(product_time_budget_sec=999, engagement_decay_sec=999),
    )


def test_cross_product_question_is_one_turn_excursion_without_losing_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    decision = director.decide(_routed_comments("P002", 2), now=11.0)

    assert (decision.product_id, decision.excursion, decision.resume_product_id) == (
        "P002",
        True,
        "P004",
    )
    decision.prepared_script = "Serum có giá ưu đãi hôm nay."
    director.mark_spoken(decision)
    assert director.state.current_product().product_id == "P004"
    assert director.state.current_product().stage_turn_index == 2


def test_strong_cross_product_demand_pivots_then_resumes_exact_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    hot = _routed_comments("P002", 8) + _routed_comments("P004", 4)

    enter = director.decide(hot, now=11.0)
    assert (enter.product_id, enter.pivot, enter.resume_product_id) == (
        "P002",
        True,
        "P004",
    )
    assert director.state.current_product().product_id == "P004"

    enter.prepared_script = "Serum đang được cả nhà hỏi rất nhiều."
    director.mark_spoken(enter)
    assert director.state.current_product().product_id == "P002"
    assert director.state.cursor.pivot_active is True

    cooled = _routed_comments("P002", 4, start=20) + _routed_comments("P004", 6, start=20)
    premature = director.decide(cooled, now=12.0)
    assert premature.action != "resume_product"

    pivot_product = director.state.current_product()
    pivot_product.is_introduced = True
    pivot_product.stage_turn_index = len(director._sales_tasks("P002"))
    resume = director.decide(cooled, now=13.0)
    assert resume.resume_product_id == "P004"
    director.mark_spoken(resume)

    assert director.state.current_product().product_id == "P004"
    assert director.state.current_product().stage_turn_index == 2
    assert director.state.cursor.pivot_active is False


def test_committing_next_product_intro_moves_real_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    first = director.state.products[0]
    first.stage_turn_index = len(director._sales_tasks("P004"))

    decision = director.decide([], now=11.0)

    assert decision.product_id == "P002"
    director.state.goto_product("P004")
    director.state.products[0].status = ProductStatus.ACTIVE
    director.state.products[1].status = ProductStatus.PENDING
    director.mark_spoken(decision)
    assert director.state.current_product().product_id == "P002"
    assert director.state.current_product().is_introduced is True
    assert director.state.products[0].status is ProductStatus.DONE
    assert director.state.products[1].status is ProductStatus.ACTIVE


def test_third_product_demand_is_queued_during_active_pivot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    director.state.cursor.pivot_active = True
    director.state.cursor.pivot_product_id = "P002"
    director.state.cursor.checkpoint_product_id = "P004"
    director.state.goto_product("P002")
    director.state.products.append(ProductState(product_id="P003", name="Kem mắt"))
    comments = [
        Comment(
            id=f"P003-{index}",
            text=f"P003 giá bao nhiêu {index}",
            embedding=[0.5, 0.5],
            t=10.0 + index / 100,
            intent="price",
            product_id="P003",
        )
        for index in range(5)
    ] + _routed_comments("P002", 6)

    decision = director.decide(comments, now=11.0)
    director.mark_spoken(decision)

    assert director.state.current_product().product_id == "P002"
    assert director.state.cursor.pivot_queue == ["P003"]


def test_decision_carries_generation_and_cache_metadata_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    decision = director_decision("answer_cluster", "P004", "Trả lời câu hỏi")
    assert decision.generation_token == 0
    assert decision.cache_variant_index is None


def test_singleton_cluster_does_not_enter_qna_ranking(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    state = StreamState(
        phase=Phase.SELLING,
        products=[ProductState(product_id="P004", name="Áo hoodie", is_introduced=True)],
    )
    director = Director(state=state)
    decision = director.decide([Comment(text="giá bao nhiêu", embedding=[1.0], t=0.0)], now=1.0)
    assert decision.action == "sell_product"


def test_qna_cluster_count_advances_only_after_playback_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    product = director.state.current_product()

    decision = director.decide(_routed_comments("P004", 2), now=11.0)

    assert product.cluster_count == 0
    director.mark_spoken(decision)
    assert product.cluster_count == 1


def test_topic_cooldown_uses_session_clock_after_long_running_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    director.state.qa_window_open = True
    director.state.qa_window_started_at = 500.0
    director.state.qa_window_stage_index = 2
    first_comments = _routed_comments("P004", 2)
    first_comments[0].t = 500.0
    first_comments[1].t = 500.1
    first = director.decide(first_comments, now=500.0)
    director.mark_spoken(first)
    assert first.action in {"answer_fact", "answer_cluster"}
    assert director.state.topic_cooldown_until["P004:price"] == 620.0
    one_new_comment = _routed_comments("P004", 1, start=10)
    one_new_comment[0].t = 501.0

    second = director.decide(first_comments + one_new_comment, now=501.0)

    assert second.action == "sell_product"


def test_answer_variant_cache_rotates_without_regeneration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    state = StreamState(
        phase=Phase.SELLING,
        products=[
            ProductState(
                product_id="P004",
                name="Áo hoodie",
                is_introduced=True,
                stage_turn_index=2,
            )
        ],
    )
    director = Director(state=state, cfg=StreamConfig(qa_topic_cooldown_sec=0))
    first_comments = _routed_comments("P004", 2)
    first = director.decide(first_comments, now=11.0)
    first.prepared_script = "Giá một trăm nghìn đồng."
    director.mark_spoken(first)
    second = director.decide(
        first_comments + _routed_comments("P004", 2, start=10),
        now=12.0,
    )

    assert second.prepared_script == "Giá một trăm nghìn đồng."
    assert second.cache_variant_index == 0
    assert second.prompt is None


def test_cached_answer_round_robin_commits_only_after_playback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    key = ("P004", "price", 0, 0)
    director.state.answer_variants[key] = ["Câu một.", "Câu hai.", "Câu ba."]
    director.state.answer_variant_index[key] = 0
    director.state.qa_window_open = True
    director.state.qa_window_started_at = 10.0
    director.state.qa_window_stage_index = 2
    comments = _routed_comments("P004", 2)

    first = director.decide(comments, now=11.0)
    assert first.prepared_script == "Câu một."
    assert director.state.answer_variant_index[key] == 0
    director.mark_spoken(first)
    assert director.state.answer_variant_index[key] == 1

    director.state.topic_cooldown_until.clear()
    expanded = comments + _routed_comments("P004", 2, start=10)
    interleaved = director.decide(expanded, now=12.0)
    director.mark_spoken(interleaved)
    second = director.decide(expanded, now=13.0)
    assert second.prepared_script == "Câu hai."


def test_qna_window_closes_early_when_no_eligible_cluster_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    director = _cross_product_director()
    director.state.qa_window_open = True
    decision = director.decide([], now=11.0)

    assert decision.action == "sell_product"
    assert director.state.qa_window_open is False


def test_cluster_answer_prompt_requests_one_clause_paraphrase_and_short_grounded_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    from backend.application.director.scoring import ScoredCluster
    from backend.application.director.clustering import Cluster

    cluster = Cluster(
        centroid=[1.0],
        members=["giá bao nhiêu", "nhiêu tiền"],
        member_ids=["a", "b"],
        intent="price",
    )
    prompt = Director(state=StreamState())._answer_prompt(ScoredCluster(cluster, 1.0, 1.0, 1.0))

    assert "một mệnh đề" in prompt
    assert "1 đến 2 câu" in prompt
    assert "không đọc lại từng comment" in prompt


def test_mock_catalog_starts_with_heygen_hoodie(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    assert MOCK_PRODUCTS[0]["id"] == "P004"


def test_sync_runtime_commits_opening_only_after_backend_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")

    class Backend(_CloudBackend):
        def __init__(self) -> None:
            self.fail = True

        def say(self, session_id: str, text: str, generate: bool = True) -> str:
            if self.fail:
                raise ConnectionError("transient")
            return text

    backend = Backend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    session_id = "sync-runtime-commit"
    runtime.attach(session_id, [MOCK_ENTITIES[0]])
    session = runtime.get_session(session_id)

    with __import__("pytest").raises(ConnectionError):
        runtime.ingest(session_id, [])
    assert session.director.state.cursor.opening_turn_index == 0

    backend.fail = False
    runtime.ingest(session_id, [])
    assert session.director.state.cursor.opening_turn_index == 1


def test_attach_ticks_remain_silent_until_first_ingest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")

    async def run() -> None:
        session_id = "stage2-silent-attach"

        class RecordingBackend(_CloudBackend):
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, bool]] = []

            def say(self, session_id: str, text: str, generate: bool = True) -> str:
                self.calls.append((session_id, text, generate))
                return text

        backend = RecordingBackend()
        runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
        coordinator = DirectorCoordinator(
            runtime=runtime,
            llm=None,
            tts=None,
            backend=backend,
            cfg=CoordinatorConfig(tick_ms=50),
        )
        coordinator.start(
            session_id,
            [MOCK_ENTITIES[0]],
            activated=False,
        )

        await coordinator._tick_once(session_id)
        await coordinator._tick_once(session_id)

        assert backend.calls == []
        coordinator.stop(session_id)

    asyncio.run(run())


def test_each_product_interleaves_sales_turns_with_viewer_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    products = [
        ProductState(product_id="P004", name="Áo hoodie HeyGen màu trắng"),
        ProductState(product_id="P001", name="Kem chống nắng"),
    ]
    state = StreamState(phase=Phase.SELLING, products=products)
    state.traffic.viewer_count = 100
    state.traffic.msg_rate = 2.0
    director = Director(state=state, cfg=StreamConfig(max_clusters_per_product=4))
    comments = [
        Comment(id="price-1", text="giá bao nhiêu", embedding=[1.0, 0.0], t=10.0),
        Comment(id="price-2", text="nhiêu tiền", embedding=[1.0, 0.0], t=10.1),
    ]

    intro = director.decide(comments, now=10.0)
    director.mark_spoken(intro)
    benefit = director.decide(comments, now=11.0)
    director.mark_spoken(benefit)
    answer = director.decide(comments, now=12.0)

    assert (intro.action, intro.product_id) == ("introduce_product", "P004")
    assert (benefit.action, benefit.stage) == ("sell_product", "benefit")
    assert answer.action in {"answer_fact", "answer_cluster"}


def test_unanswered_clusters_do_not_skip_all_product_sales_stages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    product = MOCK_ENTITIES[0]
    from backend.api.v1.router import build_run_plan

    state = StreamState(
        phase=Phase.SELLING,
        products=[ProductState(product_id=product.id, name=product.name)],
        run_plan=build_run_plan([MOCK_PRODUCTS[0]]),
    )
    state.traffic.viewer_count = 100
    state.traffic.msg_rate = 2.0
    director = Director(state=state, catalog={product.id: product})
    comments = [
        Comment(
            id=f"comment-{index}",
            text=f"áo hoodie câu hỏi {index}",
            embedding=[1.0, 0.0],
            t=10.0 + index,
        )
        for index in range(10)
    ]

    actions = []
    for now in range(10, 18):
        decision = director.decide(comments, now=float(now))
        actions.append(decision.action)
        director.mark_spoken(decision)

    assert actions[0] == "introduce_product"
    assert actions[1] == "sell_product"
    assert "answer_cluster" in actions
    assert actions.count("sell_product") >= 3


def test_answered_cluster_is_not_selected_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    state = StreamState(
        phase=Phase.SELLING,
        products=[ProductState(product_id="P004", name="Áo hoodie HeyGen màu trắng")],
    )
    state.traffic.viewer_count = 100
    state.traffic.msg_rate = 2.0
    state.products[0].is_introduced = True
    director = Director(state=state, cfg=StreamConfig(max_clusters_per_product=4))
    comments = [
        Comment(id="price-1", text="áo hoodie giá bao nhiêu", embedding=[1.0, 0.0], t=10.0),
        Comment(id="price-2", text="hoodie nhiêu tiền", embedding=[1.0, 0.0], t=10.1),
    ]

    director.state.products[0].stage_turn_index = 2
    first = director.decide(comments, now=10.0)
    director.mark_answered(first)
    expanded = comments + [
        Comment(id="price-3", text="hoodie giá sao shop", embedding=[1.0, 0.0], t=10.5)
    ]
    second = director.decide(expanded, now=11.0)

    assert first.action in {"answer_fact", "answer_cluster"}
    assert second.action in {"sell_product", "close"}


def test_coordinator_keeps_generated_script_and_selected_cluster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")

    async def run() -> None:
        session_id = "stage2-spoken-script"

        class SpeakingBackend(_CloudBackend):
            def say(self, session_id: str, text: str, generate: bool = True) -> str:
                return "Mũ lưỡi trai có giá chín mươi chín nghìn đồng."

        backend = SpeakingBackend()
        runtime = DirectorRuntime(backend=backend)
        coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
        coordinator._stats[session_id] = coordinator_stats()
        coordinator._speech_queue[session_id] = __import__("collections").deque()
        decision = director_decision("answer_cluster", "P004", "Prompt trả lời")
        decision.cluster_members = ("Mũ lưỡi trai giá bao nhiêu?",)

        await coordinator._maybe_speak(session_id, decision)

        completed = coordinator.stats(session_id)["speech_queue"]["completed"]
        assert completed["script"] == "Mũ lưỡi trai có giá chín mươi chín nghìn đồng."
        # No prompt text, customer data, or comment text in successful speech items.
        assert "prompt_layers" not in completed
        assert "selected_cluster" not in completed
        assert "prompt" not in completed
        assert "verbatim_input" not in completed

    asyncio.run(run())


def test_coordinator_speech_status_tracks_current_utterance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")

    async def run() -> None:
        session_id = "stage2-current"
        backend = _CloudBackend()
        runtime = DirectorRuntime(backend=backend)
        coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
        coordinator._stats[session_id] = coordinator_stats()
        coordinator._speech_queue[session_id] = __import__("collections").deque()
        decision = director_decision("introduce_product", "P004", "Giới thiệu P004")

        await coordinator._maybe_speak(session_id, decision)

        assert coordinator.stats(session_id)["speech_queue"]["current"] is None

    asyncio.run(run())


def coordinator_stats():
    from backend.application.director.coordinator import _SessionStats

    return _SessionStats()


def test_speech_plan_shows_current_product_and_next_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    session_id = "stage2-plan"
    backend = MockRenderBackend()
    runtime = DirectorRuntime(backend=backend)
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    products = [MOCK_ENTITIES[0], MOCK_ENTITIES[1]]
    runtime.attach(session_id, products)
    coordinator._current_speech[session_id] = director_decision(
        "introduce_product", "P004", "Giới thiệu P004"
    )

    plan = coordinator.speech_plan(session_id)

    assert plan["current"]["product_id"] == "P004"
    assert plan["current_product"]["product_id"] == "P004"
    assert plan["next_product"]["product_id"] == "P001"


def test_coordinator_stats_exposes_current_and_upcoming_speech(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    session_id = "stage2-queue"
    backend = MockRenderBackend()
    runtime = DirectorRuntime(backend=backend)
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    current = director_decision("introduce_product", "P004", "Giới thiệu P004")
    upcoming = director_decision("answer_cluster", "P004", "Trả lời giá")
    coordinator._current_speech[session_id] = current
    coordinator._speech_queue[session_id] = __import__("collections").deque([upcoming])

    speech = coordinator.stats(session_id)["speech_queue"]

    assert speech["current"]["action"] == "introduce_product"
    assert speech["current"]["product_id"] == "P004"
    assert speech["upcoming"][0]["action"] == "answer_cluster"
    assert speech["upcoming"][0]["product_id"] == "P004"


def director_decision(action: str, product_id: str, prompt: str):
    from backend.application.director.decision import Decision

    return Decision(action=action, product_id=product_id, prompt=prompt)


def test_coordinator_updates_live_traffic_before_first_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    session_id = "stage2-traffic"
    backend = MockRenderBackend()
    runtime = DirectorRuntime(backend=backend)
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    runtime.attach(session_id, [MOCK_ENTITIES[0]])

    coordinator.update_traffic(session_id, viewer_count=1010, msg_rate=2.0)

    traffic = runtime._sessions[session_id].director.state.traffic
    assert (traffic.viewer_count, traffic.msg_rate) == (1010, 2.0)


def test_locked_intro_prevents_director_from_consuming_comments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")

    async def run() -> None:
        session_id = "stage2-sequence"
        backend = MockRenderBackend()
        session = _MockSession(session_id=session_id)
        session.idle_loop_frames = backend._build_idle_loop(session_id)
        backend._sessions[session_id] = session
        runtime = DirectorRuntime(backend=backend)
        locks = SessionLockRegistry()
        coordinator = DirectorCoordinator(
            runtime=runtime,
            llm=None,
            tts=None,
            backend=backend,
            lock_registry=locks,
            cfg=CoordinatorConfig(tick_ms=50, window_sec=75.0),
        )
        products = [MOCK_ENTITIES[0], MOCK_ENTITIES[1]]
        coordinator.start(session_id, products)
        coordinator.ingest(session_id, "áo hoodie giá bao nhiêu", "viewer", ts=time.time())
        assert locks.try_acquire(session_id)

        await coordinator._tick_once(session_id)

        state = runtime._sessions[session_id].director.state
        assert [comment.text for comment in state.rolling_comments] == ["áo hoodie giá bao nhiêu"]
        assert coordinator.stats(session_id)["decisions_emitted"] == 0
        locks.release(session_id)
        coordinator.stop(session_id)

    asyncio.run(run())
