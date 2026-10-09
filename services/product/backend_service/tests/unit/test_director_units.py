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
