"""Tests for DirectorCoordinator timer bookkeeping."""

from __future__ import annotations

import asyncio
import time

import pytest

from backend.api.v1 import ProductEntityIn
from backend.application.entity.models import EntityDocument
from backend.application.director.clustering import Comment
from backend.application.director.config import StreamConfig
from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator
from backend.application.director.decision import Director
from backend.application.director.hooks import HookPool
from backend.application.director.session_context import DirectorRuntime, DirectorSession
from backend.application.director.state import (
    WINDOW_SLACK_SEC,
    Phase,
    ProductState,
    ProductStatus,
    StreamState,
)


def test_stage2_runtime_config_validates_qna_and_pivot_defaults() -> None:
    cfg = StreamConfig()
    cfg.validate_runtime()
    assert (cfg.max_qa_clusters_per_window, cfg.qa_window_hard_timeout_sec) == (2, 45.0)
    assert (cfg.demand_pivot_enter_share, cfg.demand_pivot_exit_share) == (0.60, 0.45)


def test_add_comments_prunes_history_outside_decision_horizon() -> None:
    """5.10: rolling comments + embeddings stay bounded to the decision window."""
    state = StreamState()
    state.add_comments(
        [
            Comment(text="cũ", embedding=[0.0], t=-10.0),
            Comment(text="mới", embedding=[1.0], t=70.0),
            Comment(text="cận mốc", embedding=[2.0], t=70.0),
        ]
    )
    state.embeddings_cache.update(
        {comment.id: comment.embedding for comment in state.rolling_comments}
    )

    state.prune_history(now=75.0, window_sec=75.0)

    assert [comment.text for comment in state.rolling_comments] == ["mới", "cận mốc"]
    assert set(state.embeddings_cache) == {comment.id for comment in state.rolling_comments}


@pytest.mark.asyncio
async def test_long_session_keeps_rolling_history_bounded(
    coordinator: DirectorCoordinator, runtime: DirectorRuntime
) -> None:
    """5.10: a long synthetic session never grows comment/embedding history."""
    session_id = "test-bounded-history"
    session, clock = _make_session()
    _inject_session(runtime, session_id, session)
    coordinator.start(session_id, [_entity()])
    try:
        for offset in range(30):
            clock.set(60.0 + offset * 10.0)
            coordinator.ingest(
                session_id, f"comment {offset}", "viewer", ts=time.time() - offset * 10.0
            )
            await coordinator._tick_once(session_id)

        window_bound = 2 * session.director.cfg.selection_window_sec + WINDOW_SLACK_SEC
        state = session.director.state
        assert len(state.rolling_comments) <= window_bound
        assert len(state.embeddings_cache) == len(state.rolling_comments)
        # Only comments inside the decision horizon survive.
        assert all(
            state.rolling_comments[-1].t - comment.t <= window_bound
            for comment in state.rolling_comments
        )
    finally:
        await _stop(coordinator, session_id)


class _FakeClock:
    def __init__(self, initial: float = 0.0) -> None:
        self._t = initial

    def set(self, value: float) -> None:
        self._t = value

    def now(self) -> float:
        return self._t


class _VectorEmbedder:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self.vector[:] for _ in texts]


async def _ignore_speak(session_id: str, decision: object) -> None:
    return None


async def _stop(coordinator: DirectorCoordinator, session_id: str) -> None:
    task = coordinator._tasks.get(session_id)
    coordinator.stop(session_id)
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


def _entity(**kwargs: object) -> EntityDocument:
    """One minimal product entity (8.12: entity API model, no legacy dicts)."""
    defaults: dict[str, object] = {"id": "p1", "name": "Product 1"}
    return ProductEntityIn(**{**defaults, **kwargs}).to_entity()


def _make_session(
    products: list[EntityDocument] | None = None,
    cfg: StreamConfig | None = None,
    clock: _FakeClock | None = None,
) -> tuple[DirectorSession, _FakeClock]:
    products = products or [_entity()]
    cfg = cfg or StreamConfig()
    clock = clock or _FakeClock()
    catalog = {product.id: product for product in products}
    product_states = [
        ProductState(
            product_id=product.id,
            name=product.name,
        )
        for product in products
    ]
    state = StreamState(products=product_states)
    director = Director(state=state, cfg=cfg, hook_pool=HookPool(), catalog=catalog)
    session = DirectorSession(director=director, embedder=object(), t0=0.0)
    session.now = clock.now  # type: ignore[method-assign]
    return session, clock


def _inject_session(runtime: DirectorRuntime, session_id: str, session: DirectorSession) -> None:
    runtime._sessions[session_id] = session


@pytest.fixture
def runtime() -> DirectorRuntime:
    return DirectorRuntime(backend=object())


@pytest.fixture
def coordinator(runtime: DirectorRuntime, monkeypatch: pytest.MonkeyPatch) -> DirectorCoordinator:
    # Force hashing embedder for offline tests: sessions start() via runtime.attach
    # embed the catalog with build_embedder() (default semantic), which hard-imports
    # sentence_transformers — not installed in the service dev deps.
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    coordinator = DirectorCoordinator(
        runtime=runtime,
        llm=object(),
        tts=object(),
        backend=object(),
        cfg=CoordinatorConfig(tick_ms=100),
    )
    coordinator._maybe_speak = _ignore_speak  # type: ignore[method-assign]
    return coordinator


class TestDirectorTimers:
    @pytest.mark.asyncio
    async def test_counters_advance_by_exact_delta(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-advance"
        session, clock = _make_session()
        _inject_session(runtime, session_id, session)
        products = [_entity()]
        coordinator.start(session_id, products)
        try:
            state = session.director.state
            clock.set(1.0)
            await coordinator._tick_once(session_id)
            clock.set(4.0)
            await coordinator._tick_once(session_id)
            clock.set(9.0)
            await coordinator._tick_once(session_id)
            counters = (
                state.phase_elapsed_sec,
                state.product_elapsed_sec,
                state.sec_since_relevant_msg,
            )
            assert counters == pytest.approx((9.0, 9.0, 9.0))
        finally:
            await _stop(coordinator, session_id)

    @pytest.mark.asyncio
    async def test_backward_clock_does_not_double_count(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-negative"
        session, clock = _make_session()
        _inject_session(runtime, session_id, session)
        products = [_entity()]
        coordinator.start(session_id, products)
        try:
            state = session.director.state
            clock.set(5.0)
            await coordinator._tick_once(session_id)
            clock.set(3.0)
            await coordinator._tick_once(session_id)
            clock.set(8.0)
            await coordinator._tick_once(session_id)
            counters = (
                state.phase_elapsed_sec,
                state.product_elapsed_sec,
                state.sec_since_relevant_msg,
            )
            assert counters == pytest.approx((8.0, 8.0, 8.0))
        finally:
            await _stop(coordinator, session_id)

    @pytest.mark.asyncio
    async def test_product_time_budget_switches_at_exact_boundary(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-budget"
        products = [_entity(id="p1", name="P1"), _entity(id="p2", name="P2")]
        cfg = StreamConfig(product_time_budget_sec=5.0)
        session, clock = _make_session(products=products, cfg=cfg)
        _inject_session(runtime, session_id, session)
        coordinator.start(session_id, products)
        try:
            state = session.director.state
            state.phase = Phase.SELLING
            state.products[0].status = ProductStatus.ACTIVE
            clock.set(3.0)
            await coordinator._tick_once(session_id)
            clock.set(5.0)
            await coordinator._tick_once(session_id)
            await asyncio.sleep(0)
            result = (
                state.current_product_index,
                state.product_elapsed_sec,
                coordinator.stats(session_id)["director_cycles"] >= 3,
            )
            assert result == (0, pytest.approx(5.0), True)
        finally:
            await _stop(coordinator, session_id)

    @pytest.mark.asyncio
    async def test_engagement_decay_switches_at_exact_boundary(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-engagement-boundary"
        products = [_entity(id="p1", name="P1"), _entity(id="p2", name="P2")]
        cfg = StreamConfig(engagement_decay_sec=5.0, product_time_budget_sec=100.0)
        session, clock = _make_session(products=products, cfg=cfg)
        _inject_session(runtime, session_id, session)
        coordinator.start(session_id, products)
        try:
            state = session.director.state
            state.phase = Phase.SELLING
            state.products[0].status = ProductStatus.ACTIVE
            clock.set(5.0)
            await coordinator._tick_once(session_id)
            pending = [
                *coordinator._decision_queue[session_id],
                *coordinator._speech_queue[session_id],
            ]
            result = (
                state.current_product_index,
                state.product_elapsed_sec,
                state.sec_since_relevant_msg,
                any(decision.product_id == "p2" for decision in pending),
            )
            assert result == (
                0,
                pytest.approx(5.0),
                pytest.approx(5.0),
                True,
            )
        finally:
            await _stop(coordinator, session_id)

    @pytest.mark.asyncio
    async def test_no_comment_ticks_advance_engagement_timer(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-no-comment"
        session, clock = _make_session()
        _inject_session(runtime, session_id, session)
        products = [_entity()]
        coordinator.start(session_id, products)
        try:
            state = session.director.state
            state.phase = Phase.SELLING
            clock.set(2.0)
            await coordinator._tick_once(session_id)
            clock.set(4.0)
            await coordinator._tick_once(session_id)
            clock.set(6.0)
            await coordinator._tick_once(session_id)
            assert state.sec_since_relevant_msg == pytest.approx(6.0)
        finally:
            await _stop(coordinator, session_id)

    def test_fresh_relevant_comment_prevents_decay_switch(self) -> None:
        product = _entity(id="p1", name="P1", price=100)
        state = StreamState(
            phase=Phase.SELLING,
            products=[
                ProductState(
                    product_id="p1",
                    name="P1",
                    status=ProductStatus.ACTIVE,
                    embedding=[1.0, 0.0],
                    is_introduced=True,
                )
            ],
            sec_since_relevant_msg=5.0,
        )
        director = Director(
            state=state,
            cfg=StreamConfig(engagement_decay_sec=5.0, product_time_budget_sec=100.0),
            hook_pool=HookPool(),
            catalog={product.id: product},
        )

        state.products[0].stage_turn_index = 2
        decision = director.decide(
            [
                Comment(
                    text="P1 giá bao nhiêu?",
                    embedding=[1.0, 0.0],
                    t=10.0,
                    intent="price",
                    product_id="p1",
                ),
                Comment(
                    text="P1 nhiêu tiền?",
                    embedding=[1.0, 0.0],
                    t=10.1,
                    intent="price",
                    product_id="p1",
                ),
            ],
            now=10.0,
        )

        assert (decision.action, state.phase, state.sec_since_relevant_msg) == (
            "answer_fact",
            Phase.SELLING,
            pytest.approx(0.0),
        )

    def test_relevant_cluster_resets_engagement_timer(self) -> None:
        product = _entity(id="p1", name="P1", price=100)
        state = StreamState(
            phase=Phase.SELLING,
            products=[
                ProductState(
                    product_id="p1",
                    name="P1",
                    embedding=[1.0, 0.0],
                    is_introduced=True,
                )
            ],
            sec_since_relevant_msg=10.0,
        )
        director = Director(
            state=state,
            cfg=StreamConfig(),
            hook_pool=HookPool(),
            catalog={product.id: product},
        )
        state.products[0].stage_turn_index = 2
        decision = director.decide(
            [
                Comment(text="giá bao nhiêu", embedding=[1.0, 0.0], t=0.0),
                Comment(text="nhiêu tiền", embedding=[1.0, 0.0], t=0.1),
            ],
            now=5.0,
        )
        result = (decision.action, decision.product_id, state.sec_since_relevant_msg)
        assert result == ("answer_fact", "p1", pytest.approx(0.0))

    @pytest.mark.asyncio
    async def test_off_topic_comment_does_not_reset_engagement_timer(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-off-topic"
        product = _entity(id="p1", name="P1")
        session, clock = _make_session(products=[product])
        _inject_session(runtime, session_id, session)
        coordinator._embedder = _VectorEmbedder([0.0, 1.0])
        coordinator.start(session_id, [product])
        try:
            state = session.director.state
            state.phase = Phase.SELLING
            clock.set(5.0)
            coordinator.ingest(session_id, "unrelated topic", "viewer", ts=time.time())
            await coordinator._tick_once(session_id)
            result = (
                state.sec_since_relevant_msg,
                state.current_product_index,
                state.rolling_comments[-1].text,
            )
            assert result == (pytest.approx(5.0), 0, "unrelated topic")
        finally:
            await _stop(coordinator, session_id)

    @pytest.mark.asyncio
    async def test_start_is_idempotent_and_uses_clock_baseline(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-idempotent"
        session, clock = _make_session(clock=_FakeClock(initial=10.0))
        _inject_session(runtime, session_id, session)
        products = [_entity()]
        coordinator.start(session_id, products)
        try:
            clock.set(12.0)
            await coordinator._tick_once(session_id)
            coordinator.start(session_id, products)
            clock.set(15.0)
            await coordinator._tick_once(session_id)
            result = (session.director.state.phase_elapsed_sec, coordinator._last_tick[session_id])
            assert result == (pytest.approx(5.0), pytest.approx(15.0))
        finally:
            await _stop(coordinator, session_id)

    @pytest.mark.asyncio
    async def test_stop_cleans_timer_bookkeeping(
        self, coordinator: DirectorCoordinator, runtime: DirectorRuntime
    ) -> None:
        session_id = "test-stop"
        session, _ = _make_session()
        _inject_session(runtime, session_id, session)
        products = [_entity()]
        coordinator.start(session_id, products)
        task = coordinator._tasks[session_id]
        try:
            await asyncio.sleep(0)
        finally:
            await _stop(coordinator, session_id)
        assert (session_id not in coordinator._last_tick, task.done()) == (True, True)
