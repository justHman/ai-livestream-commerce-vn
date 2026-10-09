"""Director cursor over ordered approved units: pivot, closing, re-attach, legacy."""

from __future__ import annotations

from backend.application.director.decision import Decision, Director
from backend.application.director.state import Phase, ProductState, StreamState

P1 = ["P1 mở đầu.", "P1 giới thiệu.", "P1 điểm nổi bật.", "P1 chốt đơn."]
P2 = ["P2 giới thiệu.", "P2 điểm nổi bật.", "P2 chốt đơn.", "P2 lời kết."]


def _director(*unit_lists) -> Director:
    products = [
        ProductState(product_id=f"P{i + 1}", name=f"P{i + 1}", units=tuple(units))
        for i, units in enumerate(unit_lists)
    ]
    state = StreamState(products=products)
    director = Director(state)
    state.phase = Phase.SELLING
    state.cursor.opening_completed = True
    return director


def _play(director: Director, limit: int = 20) -> list[str]:
    spoken = []
    for step in range(limit):
        decision = director.decide([], now=float(step))
        if decision.action in ("idle", "skip"):
            break
        spoken.append(decision.prepared_script)
        director.mark_spoken(decision)
    return spoken


def test_pivot_to_last_product_never_speaks_closing_before_other_units() -> None:
    d = _director(P1, P2)
    d.mark_spoken(
        Decision(action="autonomous_opening", stage="opening", product_id="P1", unit_index=0)
    )
    first = d.decide([], now=1.0)
    d.mark_spoken(first)
    d._start_pivot("P2")  # demand pivots to the last product while P1 is half played
    spoken = [first.prepared_script, *_play(d)]
    assert spoken == [P1[1], *P2[:3], P1[2], P1[3], P2[3]]


def test_closing_waits_for_every_products_units() -> None:
    d = _director(P1, P2)
    d.state.current_product_index = 1  # a pivot left us on the last product, P1 untouched
    d.state.products[1].is_introduced = False
    spoken = _play(d)
    assert spoken[-1] == P2[3]
    assert spoken.index(P2[3]) == len(spoken) - 1
    assert set(P1) - {P1[0]} <= set(spoken)  # P1's remaining units all played before closing
    assert spoken.count(P2[3]) == 1


def test_cursor_survives_reattach_and_never_replays() -> None:
    d = _director(P1, P2)
    spoken = []
    for step in range(3):
        decision = d.decide([], now=float(step))
        spoken.append(decision.prepared_script)
        d.mark_spoken(decision)
    # re-attach rebuilds ProductState but copies next_unit (session_context.attach)
    carried = d.state.products[0].next_unit
    assert carried == 3
    rebuilt = ProductState(product_id="P1", name="P1", units=tuple(P1), next_unit=carried)
    d.state.products[0] = rebuilt
    spoken += _play(d)
    assert spoken == [*P1, *P2]


def test_legacy_single_unit_spoken_once() -> None:
    d = _director(["Cả bài một đoạn."], ["Bài hai một đoạn."])
    assert _play(d)[:2] == ["Cả bài một đoạn.", "Bài hai một đoạn."]
    assert _play(d).count("Cả bài một đoạn.") == 0


def test_qa_between_units_does_not_move_the_cursor() -> None:
    d = _director(P1, P2)
    first = d.decide([], now=0.0)
    d.mark_spoken(first)
    d.mark_spoken(
        Decision(action="answer_fact", product_id="P1", stage="qa", cluster_member_ids=("c1",))
    )
    assert d.decide([], now=2.0).prepared_script == P1[1]


def _hot_director(order_locked: bool) -> Director:
    import os

    os.environ["DIRECTOR_EMBEDDER"] = "hash"
    from backend.application.director.config import StreamConfig

    products = [
        ProductState(
            product_id="P004",
            name="A",
            units=tuple(P1),
            next_unit=2,
            is_introduced=True,
            stage_turn_index=2,
        ),
        ProductState(product_id="P002", name="B", units=tuple(P2)),
    ]
    state = StreamState(phase=Phase.SELLING, products=products)
    state.cursor.opening_completed = True
    director = Director(
        state, cfg=StreamConfig(product_time_budget_sec=999, engagement_decay_sec=999)
    )
    director.order_locked = order_locked
    return director


def test_order_locked_set_answers_other_product_without_leaving_owner_order() -> None:
    from .test_director_decisions import _routed_comments

    hot = _routed_comments("P002", 8) + _routed_comments("P004", 4)
    free = _hot_director(False).decide(hot, now=11.0)
    assert free.pivot is True  # ORDER_AGNOSTIC behaviour is unchanged

    d = _hot_director(True)
    answer = d.decide(hot, now=11.0)
    assert (answer.product_id, answer.pivot, answer.excursion) == ("P002", False, True)
    answer.prepared_script = "Câu trả lời đã duyệt."
    d.mark_spoken(answer)
    assert d.state.current_product().product_id == "P004"
    assert d.state.cursor.pivot_active is False
    assert _play(d) == [P1[2], P1[3], *P2]  # owner order: P1 to its end, then P2


def test_rejected_unit_is_not_skipped_by_a_later_completion() -> None:
    d = _director(P1, P2)
    skipped = d.decide([], now=0.0)  # unit 0 is rejected downstream: never marked
    later = Decision(
        action="sell_product", product_id="P1", stage="benefit", unit_index=1, prepared_script="x"
    )
    d.mark_spoken(later)
    assert d.state.products[0].next_unit == 0
    assert d.decide([], now=1.0).prepared_script == skipped.prepared_script


def test_binding_an_order_aware_envelope_locks_the_director_order() -> None:
    import json
    from types import SimpleNamespace

    from backend.application.director.session_context import DirectorSession

    def bind(policy):
        director = _director(P1, P2)
        session = DirectorSession(director=director, embedder=None)
        envelope = SimpleNamespace(
            brief_json=json.dumps({"transition_policy": policy}),
            products=[SimpleNamespace(product_id="P1", units=tuple(P1))],
        )
        session.bind_envelope(envelope)
        return director.order_locked

    assert bind("ORDER_AWARE") is True
    assert bind("ORDER_AGNOSTIC") is False


def test_reattach_carries_units_with_their_cursor() -> None:
    from backend.api.v1 import ProductEntityIn
    from backend.application.director.embeddings import HashingEmbedder
    from backend.application.director.session_context import DirectorRuntime
    from backend.application.render.engines_base import FullPipelineBackend

    class Backend(FullPipelineBackend):
        name = "b"

        def start(self, opts): ...

        def say(self, session_id, text, generate=True):
            return text

        def interrupt(self, session_id): ...

        def stop(self, session_id): ...

    runtime = DirectorRuntime(backend=Backend(), embedder=HashingEmbedder())
    entity = ProductEntityIn(id="P004", name="A").to_entity()
    runtime.attach("s", [entity])
    product = runtime.get_session("s").director.state.products[0]
    product.units, product.next_unit = tuple(P1), 2
    runtime.attach("s", [entity])  # re-attach rebuilds ProductState
    again = runtime.get_session("s").director.state.products[0]
    assert (again.units, again.next_unit) == (tuple(P1), 2)


def test_rebind_restarts_only_products_whose_approved_text_changed() -> None:
    import json
    from types import SimpleNamespace

    from backend.application.director.session_context import DirectorSession

    d = _director(P1, P2)
    session = DirectorSession(director=d, embedder=None)

    def env(p1_text, policy="ORDER_AGNOSTIC"):
        return SimpleNamespace(
            brief_json=json.dumps({"transition_policy": policy}),
            products=[
                SimpleNamespace(
                    product_id="P1",
                    approved_version_id=p1_text,
                    spoken_text=p1_text,
                    units=tuple(P1),
                ),
                SimpleNamespace(
                    product_id="P2",
                    approved_version_id="v2",
                    spoken_text="same",
                    units=tuple(P2),
                ),
            ],
        )

    session.bind_envelope(env("v1"))
    d.state.products[0].next_unit = 3
    d.state.products[1].next_unit = 2
    session.bind_envelope(env("v1-edited", policy="ORDER_AWARE"))
    assert (d.state.products[0].next_unit, d.state.products[1].next_unit) == (0, 2)
    assert d.order_locked is True
