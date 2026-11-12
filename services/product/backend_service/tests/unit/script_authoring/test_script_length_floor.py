"""Per-product duration floor, recap rounds and periodic price/CTA reminders (plan level, no LLM)."""

from __future__ import annotations

from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    SessionBrief,
    _duration_s,
    build_unit_prompt,
    fallback_unit_text,
    plan_units,
    product_target_s,
)


def _product(n: int, code: str = "", price: bool = True) -> ProductBrief:
    claims = tuple(f"Điểm nổi bật số {chr(97 + i)} của sản phẩm này rất rõ ràng." for i in range(n))
    return ProductBrief(
        product_id="p",
        name="Áo thun",
        prices=("199000",) if price else (),
        discounts=("Giảm 10% cho đơn đầu tiên.",) if price else (),
        claims=claims,
        order_code=code,
    )


def _plan(product, target=360, first=True, last=True):
    return plan_units(product, first=first, last=last, ordered_aware=True, target_duration_s=target)


def _planned_s(units) -> float:
    return sum(u.target_seconds for u in units)


def test_formula():
    assert product_target_s(360, 13) == 325  # 25 s/fact ceiling, floor 240
    assert product_target_s(360, 3) == 120  # floor never below 120
    assert product_target_s(360, 6) == 150
    assert product_target_s(360, 9) == 225  # floor 180, 25 s/fact ceiling wins
    assert product_target_s(360, 12) == 300  # floor 240, 25 s/fact ceiling wins
    assert product_target_s(360, 40) == 360  # policy wins
    assert product_target_s(100, 13) == 100  # requested below the floor wins


def test_thirteen_facts_reach_floor():
    units = _plan(_product(12))  # 12 claims + 1 discount = 13 facts
    assert _planned_s(units) >= 0.7 * 240
    assert all(u.target_seconds <= 60 for u in units)


def test_few_facts_stay_short_and_many_capped():
    assert _planned_s(_plan(_product(3))) <= 0.85 * 150 + 1
    assert _planned_s(_plan(_product(40), target=360)) <= 360


def test_recaps_come_before_splitting_parts():
    units = _plan(_product(12))
    assert max(len(u.claims) for u in units if u.role == "highlight") == 4


def test_recaps_use_only_approved_facts_and_never_repeat_a_part():
    p = _product(12)
    units = _plan(p)
    approved = set(p.claims)
    recaps = [u for u in units if u.revisit]
    assert recaps, "recap rounds expected"
    assert all(set(u.claims) <= approved for u in recaps)
    assert all(not set(u.promos) - set(p.discounts) for u in recaps)
    keys = [(u.role, u.claims, u.promos) for u in recaps]
    assert len(keys) == len(set(keys))


def test_order_code_only_when_enabled():
    s = SessionBrief()
    for code, expect in (("", False), ("G30", True)):
        p = _product(40, code)
        units = _plan(p)
        assert any(u.role == "offer" and u.revisit and u.with_cta for u in units)
        for i, u in enumerate(units):
            text = build_unit_prompt(s, p, units, i)
            assert ("Chốt G30 1" in text) is (expect and u.with_cta)


def test_reminders_follow_cumulative_spoken_seconds():
    p = _product(40)
    units = _plan(p)
    durations = [max(u.target_seconds, _duration_s(fallback_unit_text(u, p))) for u in units]
    reminders = [i for i, u in enumerate(units) if u.role == "offer" and u.revisit]
    assert reminders, "a 360 s product needs a mid-script price/CTA reminder"
    assert _planned_s(units) <= 360
    elapsed_s = 0.0
    for i, u in enumerate(units):
        if i in reminders:
            assert 150 <= elapsed_s <= 150 + max(durations[:i])
            assert sum(durations[i + 1 :]) >= 45
            assert units[i - 1].role != "offer"
            assert units[i + 1].role != "offer"
            assert u.with_cta and u.with_price
            assert set(u.promos) <= set(p.discounts)
        elapsed_s = 0.0 if u.role == "offer" else elapsed_s + durations[i]


def test_short_scripts_and_products_without_offers_get_no_reminders():
    for p, target in ((_product(4), 140), (_product(3), 360), (_product(40, price=False), 360)):
        assert not any(u.role == "offer" and u.revisit for u in _plan(p, target=target))
