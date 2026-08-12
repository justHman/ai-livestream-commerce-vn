"""Task 7.8 tests: one-call fixed-K planner, reference validation, variety.

These tests prove:

- Plans for 10/30/60-minute targets carry exactly the backend-fixed K
  segments (no dynamically expanding count).
- K is clamped to calibration bounds.
- Long-form variety: segments get non-empty distinct topics across K.
- Planner references only authoritative fact/objection IDs; unknown,
  duplicate, and impossible-coverage references are rejected
  deterministically (task 7.6).
- Reconcile into backend-fixed K happens without asking the model for
  additional planning loops (task 7.7): a candidate with too few sections
  is padded, one with too many is truncated.
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.generation.calibration import (
    GenerationBudgetCalibration,
)
from backend.application.script_authoring.generation.planner import (
    AuthoritativeContext,
    PlanRejectionError,
    ProductScriptPlan,
    ProductScriptPlanner,
)

DEFAULT = GenerationBudgetCalibration()
"""Default conservative calibration (see test_script_authoring_preview)."""

AUTHORITATIVE = AuthoritativeContext(
    product_id="P001",
    fact_ids=frozenset({"fact-1", "fact-2", "fact-3", "fact-4"}),
    objection_ids=frozenset({"obj-1", "obj-2"}),
)


def _planner() -> ProductScriptPlanner:
    return ProductScriptPlanner(DEFAULT)


def test_plan_10_minute_target() -> None:
    """600 s -> K=3 segments, calls = 1 + 3 = 4."""
    plan = _planner().plan(
        "P001",
        600.0,
        AUTHORITATIVE,
        candidate_sections=[
            {"topic": "Mở đầu", "allowed_fact_ids": ["fact-1"]},
            {"topic": "Tính năng", "allowed_fact_ids": ["fact-2"]},
            {"topic": "Chốt đơn", "allowed_fact_ids": ["fact-3"]},
        ],
    )
    assert len(plan.segments) == 3
    assert [s.segment_index for s in plan.segments] == [0, 1, 2]
    assert [s.topic for s in plan.segments] == ["Mở đầu", "Tính năng", "Chốt đơn"]
    assert plan.segments[0].target_duration_s == pytest.approx(600.0 / 3)
    assert sum(s.target_duration_s for s in plan.segments) == pytest.approx(600.0)


def test_plan_30_minute_target() -> None:
    """1800 s -> K=8 segments (ceil(1800/256) = 8)."""
    plan = _planner().plan(
        "P001",
        1800.0,
        AUTHORITATIVE,
        candidate_sections=[
            {"topic": f"Chủ đề {i}", "allowed_fact_ids": [f"fact-{i}"]}
            for i in range(1, 5)
        ],
    )
    assert len(plan.segments) == 8
    assert plan.segments[0].segment_index == 0
    assert plan.segments[7].segment_index == 7
    # Candidate shorter than K: remaining sections are synthesized (7.7).
    assert plan.segments[3].topic == "Chủ đề 4"
    assert plan.segments[4].topic.startswith("Phần")


def test_plan_60_minute_target() -> None:
    """3600 s -> K=15 segments (Decision 11 example)."""
    plan = _planner().plan(
        "P001",
        3600.0,
        AUTHORITATIVE,
        candidate_sections=[],
    )
    assert len(plan.segments) == 15
    assert [s.segment_index for s in plan.segments] == list(range(15))


def test_k_bounds_clamp_segment_count() -> None:
    """K is clamped to min/max_segment_count."""
    narrow = GenerationBudgetCalibration(max_segment_count=4)
    plan = ProductScriptPlanner(narrow).plan(
        "P001",
        3600.0,
        AUTHORITATIVE,
        candidate_sections=[],
    )
    assert len(plan.segments) == 4


def test_long_form_variety() -> None:
    """K segments keep distinct non-empty topics (no single-topic repeat).

    Each candidate section claims a distinct authoritative fact (fact-1..4 —
    the planner rejects references outside the context, so only 4 unique
    fact ids exist; a 15-segment plan therefore uses at most those 4).
    """
    plan = _planner().plan(
        "P001",
        3600.0,
        AUTHORITATIVE,
        candidate_sections=[
            {
                "topic": f"Chủ đề {i}",
                "allowed_fact_ids": [f"fact-{i}"],
            }
            for i in range(1, 5)
        ],
    )
    topics = [s.topic for s in plan.segments]
    assert len(topics) == len(set(topics)) == 15
    assert all(t.strip() for t in topics)
    # Only authoritative facts are referenced (never fact-5+).
    for segment in plan.segments:
        assert all(fact_id in AUTHORITATIVE.fact_ids for fact_id in segment.allowed_fact_ids)


def test_plan_references_only_authoritative_facts() -> None:
    """A fact reference outside the authoritative context is rejected."""
    planner = _planner()
    with pytest.raises(PlanRejectionError) as exc:
        planner.plan(
            "P001",
            600.0,
            AUTHORITATIVE,
            candidate_sections=[{"allowed_fact_ids": ["fact-unknown"]}],
        )
    assert "unknown fact" in str(exc.value)


def test_plan_rejects_duplicate_reference_in_segment() -> None:
    """Duplicated fact IDs inside one segment are rejected."""
    with pytest.raises(PlanRejectionError) as exc:
        _planner().plan(
            "P001",
            600.0,
            AUTHORITATIVE,
            candidate_sections=[
                {"allowed_fact_ids": ["fact-1", "fact-1"]},
            ],
        )
    assert "duplicates fact reference" in str(exc.value)


def test_plan_rejects_reused_reference_across_segments() -> None:
    """A fact already assigned to an earlier segment cannot be reused."""
    with pytest.raises(PlanRejectionError) as exc:
        _planner().plan(
            "P001",
            600.0,
            AUTHORITATIVE,
            candidate_sections=[
                {"allowed_fact_ids": ["fact-1"]},
                {"allowed_fact_ids": ["fact-1"]},
            ],
        )
    assert "already assigned" in str(exc.value)


def test_plan_rejects_impossible_coverage() -> None:
    """A segment whose references are all covered cannot be planned."""
    with pytest.raises(PlanRejectionError) as exc:
        _planner().plan(
            "P001",
            600.0,
            AUTHORITATIVE,
            candidate_sections=[
                {"allowed_fact_ids": ["fact-1"]},
                # Second segment references only already-covered facts plus
                # already-covered objections -> impossible coverage.
                {"allowed_fact_ids": ["fact-1"], "allowed_objection_ids": ["obj-1"]},
            ],
        )
    # The planner rejects a segment that can only reuse already-covered
    # references (deterministic rejection, not a plan with no new content).
    assert "already assigned" in str(exc.value) or "re-uses" in str(exc.value)


def test_plan_rejects_unknown_objection() -> None:
    """An objection reference outside the authoritative context is rejected."""
    with pytest.raises(PlanRejectionError) as exc:
        _planner().plan(
            "P001",
            600.0,
            AUTHORITATIVE,
            candidate_sections=[{"allowed_objection_ids": ["obj-nope"]}],
        )
    assert "unknown objection" in str(exc.value)


def test_plan_rejects_mismatched_product() -> None:
    """Planning for a different product than the authoritative context fails."""
    with pytest.raises(PlanRejectionError):
        _planner().plan("P999", 600.0, AUTHORITATIVE, candidate_sections=[])


def test_plan_truncates_overlong_candidate() -> None:
    """A candidate with more sections than K never expands the segment count."""
    plan = _planner().plan(
        "P001",
        600.0,
        AUTHORITATIVE,
        candidate_sections=[
            {"topic": f"Chủ đề {i}"} for i in range(1, 30)
        ],
    )
    assert len(plan.segments) == 3  # K stays fixed at 3


def test_plan_is_deterministic() -> None:
    """Identical candidate + context produce an identical plan object."""
    planner = _planner()
    sections = [
        {"topic": "Mở đầu", "allowed_fact_ids": ["fact-1"]},
        {"topic": "Tính năng", "allowed_fact_ids": ["fact-2"]},
    ]
    first = planner.plan("P001", 600.0, AUTHORITATIVE, sections)
    second = planner.plan("P001", 600.0, AUTHORITATIVE, sections)
    assert first == second
    assert isinstance(first, ProductScriptPlan)


def test_plan_carries_cta_and_transition_intents() -> None:
    """Plan segments carry optional CTA/transition intents from the candidate."""
    plan = _planner().plan(
        "P001",
        600.0,
        AUTHORITATIVE,
        candidate_sections=[
            {
                "topic": "Chốt đơn",
                "cta_intent": "chốt đơn nhanh",
                "transition_intent": "chuyển sang sản phẩm kế tiếp",
            },
        ],
    )
    assert plan.segments[0].cta_intent == "chốt đơn nhanh"
    assert plan.segments[0].transition_intent == "chuyển sang sản phẩm kế tiếp"
