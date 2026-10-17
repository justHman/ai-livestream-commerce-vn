"""M9(a): a viewer question that arrives while the session is about to close must still be answered.

The closing of an ordered-unit session is the reserved last unit of the last product. It is DECIDED and prepared one
turn ahead, while the previous unit is still playing, so a question that arrives in the last ~50 seconds used to meet
a Director that had already decided to close. The cut-off defined here: a question is answered while the closing has
NOT started playing (it is still only queued); once the closing is playing, questions wait for nothing and are not promised.
"""

from __future__ import annotations

import pytest

from backend.application.director.clustering import Comment
from backend.application.director.decision import Decision
from backend.application.director.state import Phase, ProductStatus

from .test_director_coordinator import _make_coordinator, _make_products


def _ready_last_product(ds, units=("Mở đầu.", "Phần hai.", "Lời kết.")):
    """One product whose reserved closing unit is the only one left (units 0 and 1 spoken)."""
    state = ds.director.state
    state.phase = Phase.SELLING
    state.cursor.opening_completed = True
    ds.director.cfg.qa_min_cluster_size = 1
    product = state.products[-1]
    state.current_product_index = len(state.products) - 1
    for other in state.products[:-1]:
        other.units = ()
    product.units = units
    product.next_unit = len(units) - 1  # the reserved closing is the next (and last) unit
    product.is_introduced = True
    product.stage_turn_index = 2
    return state, product


def _question(ds, product, cid="c1", text="Giá bao nhiêu?"):
    return Comment(
        id=cid,
        text=text,
        embedding=[1.0, 0.0],
        t=ds.now(),
        product_id=product.product_id,
        intent="price",
    )


@pytest.mark.asyncio
async def test_question_arriving_before_the_closing_plays_is_answered_first(monkeypatch):
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "closing-qa-before"
    coord, runtime, _, backend = _make_coordinator(session_id=sid)
    coord.start(sid, _make_products())
    try:
        ds = runtime.get_session(sid)
        state, product = _ready_last_product(ds)
        state.rolling_comments = [_question(ds, product)]
        queued_close = Decision(
            action="close",
            product_id=product.product_id,
            unit_index=len(product.units) - 1,
            prepared_script=product.units[-1],
            stage="closing",
        )
        coord._speech_queue[sid].append(queued_close)  # decided and prepared ahead, not playing yet
        assert await coord._refresh_at_boundary(sid), (
            "the question must replace the queued closing at the boundary"
        )
        assert queued_close.is_cancelled
        assert coord._decision_queue[sid][0].action in ("answer_fact", "answer_cluster")
    finally:
        coord.stop(sid)
        backend.stop_all()


@pytest.mark.asyncio
async def test_a_busy_last_product_still_answers_a_fresh_question_before_the_closing(monkeypatch):
    """The per-product cluster cap used to switch to CLOSING first, discarding the fresh question."""
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "closing-qa-cap"
    coord, runtime, _, backend = _make_coordinator(session_id=sid)
    coord.start(sid, _make_products())
    try:
        ds = runtime.get_session(sid)
        state, product = _ready_last_product(ds)
        product.cluster_count = (
            ds.director.cfg.max_clusters_per_product
        )  # the last product already answered its share
        state.rolling_comments = [_question(ds, product, cid="busy")]
        queued_close = Decision(
            action="close",
            product_id=product.product_id,
            unit_index=len(product.units) - 1,
            prepared_script=product.units[-1],
            stage="closing",
        )
        coord._speech_queue[sid].append(queued_close)
        assert await coord._refresh_at_boundary(sid)
        assert coord._decision_queue[sid][0].action in ("answer_fact", "answer_cluster")
    finally:
        coord.stop(sid)
        backend.stop_all()


@pytest.mark.asyncio
async def test_time_budget_exhausted_last_product_still_answers_a_fresh_question(monkeypatch):
    monkeypatch.setenv("DIRECTOR_EMBEDDER", "hash")
    sid = "closing-qa-budget"
    coord, runtime, _, backend = _make_coordinator(session_id=sid)
    coord.start(sid, _make_products())
    try:
        ds = runtime.get_session(sid)
        state, product = _ready_last_product(ds)
        state.product_elapsed_sec = ds.director.cfg.product_time_budget_sec + 1
        state.rolling_comments = [_question(ds, product, cid="late")]
        queued_close = Decision(
            action="close",
            product_id=product.product_id,
            unit_index=len(product.units) - 1,
            prepared_script=product.units[-1],
            stage="closing",
        )
        coord._speech_queue[sid].append(queued_close)
        assert await coord._refresh_at_boundary(sid)
    finally:
        coord.stop(sid)
        backend.stop_all()


# ── Director level: bounded, exactly one closing, nothing after it ────────────────────────────


def _closing_due_director(units=("Mở đầu.", "Phần hai.", "Lời kết.")):
    from backend.application.director.decision import Director
    from backend.application.director.state import ProductState, StreamState

    product = ProductState(product_id="P1", name="P1", units=units)
    state = StreamState(products=[product])
    director = Director(state)
    director.cfg.qa_min_cluster_size = 1
    state.phase = Phase.SELLING
    state.cursor.opening_completed = True
    product.is_introduced = True
    product.next_unit = len(units) - 1  # only the reserved closing is left
    product.stage_turn_index = 2
    product.cluster_count = (
        director.cfg.max_clusters_per_product
    )  # the cap would switch to the closing at once
    return director, product


def _asked(director, i, now):
    # distinct embeddings so each question is its own cluster, distinct intents so no topic cooldown applies
    vec = [0.0] * 8
    vec[i % 8] = 1.0
    return Comment(
        id=f"q{i}",
        text=f"Câu hỏi số {i}?",
        embedding=vec,
        t=now,
        product_id="P1",
        intent=f"topic{i}",
    )


def test_pre_closing_answers_are_bounded_and_the_closing_is_spoken_once() -> None:
    director, product = _closing_due_director()
    actions = []
    for step in range(12):
        now = float(step)
        director.state.rolling_comments = [_asked(director, step, now)]
        decision = director.decide(director.state.rolling_comments, now=now)
        actions.append(decision.action)
        if decision.action in ("idle", "skip"):
            break
        director.mark_spoken(decision)
    answers = [a for a in actions if a in ("answer_fact", "answer_cluster")]
    assert 1 <= len(answers) <= director.cfg.max_closing_qa_answers
    assert actions.count("close") == 1, actions
    assert actions.index("close") > 0, (
        "at least one waiting question is answered before the closing"
    )
    assert director.state.closing_spoken


def test_no_question_after_the_closing_is_spoken_is_answered() -> None:
    director, product = _closing_due_director()
    close = None
    for step in range(10):
        decision = director.decide([], now=float(step))
        if decision.action == "close":
            close = decision
            break
    assert close is not None
    director.mark_spoken(close)
    late = _asked(director, 1, 50.0)
    director.state.rolling_comments = [late]
    assert director.decide([late], now=50.0).action == "idle"


def test_without_questions_the_closing_comes_straight_away_and_unchanged() -> None:
    director, product = _closing_due_director()
    decision = director.decide([], now=1.0)
    assert decision.action == "close" and decision.prepared_script == product.units[-1]


def test_a_question_does_not_delay_the_switch_to_the_next_product() -> None:
    """Only a switch that would END the session is deferred: with another product pending it behaves as before."""
    from backend.application.director.decision import Director
    from backend.application.director.state import ProductState, StreamState

    first = ProductState(product_id="P1", name="P1", units=("a.", "b.", "c."))
    second = ProductState(product_id="P2", name="P2", units=("d.", "e.", "f."))
    state = StreamState(products=[first, second])
    director = Director(state)
    director.cfg.qa_min_cluster_size = 1
    state.phase = Phase.SELLING
    state.cursor.opening_completed = True
    first.is_introduced = True
    first.next_unit = 2
    first.stage_turn_index = 2
    first.cluster_count = director.cfg.max_clusters_per_product
    assert director._switch_would_close() is False


@pytest.mark.asyncio
async def test_reducer_feed_answers_a_question_before_the_closing_when_the_cap_would_close() -> (
    None
):
    """The production feed (reducer selections) takes the same bounded pre-closing path as the legacy feed."""
    import time

    from backend.application.director.reducer_input import build_selections

    from .test_reducer_director_input import _comment, _reducer, _seed, _selling_director

    reducer = _reducer()
    now = time.time()
    await _seed(
        reducer,
        "s1",
        [
            _comment("c1", "cách dùng sản phẩm thế nào", now, "c1"),
            _comment("c2", "cách dùng sản phẩm thế nào", now, "c2"),
        ],
        now=now,
    )
    store = reducer.session_store("s1")
    director = _selling_director(stage_turn_index=10)
    director.cfg.qa_min_cluster_size = 1
    state = director.state
    state.current_product_index = len(state.products) - 1
    for other in state.products[:-1]:
        other.status = ProductStatus.DONE
    product = state.current_product()
    product.units = ("Mở đầu.", "Phần hai.", "Lời kết.")
    product.next_unit = 2  # only the reserved closing is left
    product.is_introduced = True
    product.stage_turn_index = 10  # past every planned stage, so the cap is what ends the product
    product.cluster_count = director.cfg.max_clusters_per_product  # the cap would close the session
    selections = build_selections(
        director, store=store, reducer_now=now, director_now=now, wall_now=now
    )
    assert selections, "the reducer must have projected the waiting question"
    decision = director.decide_from_reducer(selections, now, high_value_ids=lambda cid: False)
    assert decision.action in ("answer_cluster", "answer_fact"), decision.action
    assert decision.pre_closing is True


def test_excursion_answers_before_the_closing_are_bounded_too() -> None:
    """A stream of questions about an EARLIER product (cross-product excursions) must not postpone the closing forever."""
    from backend.application.director.decision import Director
    from backend.application.director.state import ProductState, StreamState

    first = ProductState(
        product_id="P1", name="P1", units=("a.", "b.", "c."), status=ProductStatus.DONE
    )
    first.next_unit = 2
    last = ProductState(product_id="P2", name="P2", units=("d.", "e.", "Lời kết."))
    state = StreamState(products=[first, last])
    director = Director(state)
    director.cfg.qa_min_cluster_size = 1
    state.phase = Phase.SELLING
    state.cursor.opening_completed = True
    state.current_product_index = 1
    last.status = ProductStatus.ACTIVE
    last.is_introduced = True
    last.next_unit = 2
    last.stage_turn_index = 10
    last.cluster_count = director.cfg.max_clusters_per_product
    actions = []
    for step in range(12):
        now = float(step)
        vec = [0.0] * 8
        vec[step % 8] = 1.0
        question = Comment(
            id=f"x{step}",
            text=f"Hỏi về P1 số {step}?",
            embedding=vec,
            t=now,
            product_id="P1",
            intent=f"t{step}",
        )
        state.rolling_comments = [question]
        decision = director.decide([question], now=now)
        actions.append(decision.action)
        if decision.action in ("idle", "skip"):
            break
        director.mark_spoken(decision)
    assert actions.count("close") == 1, actions
    assert (
        len([a for a in actions if a in ("answer_fact", "answer_cluster")])
        <= director.cfg.max_closing_qa_answers
    ), actions
