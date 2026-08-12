"""Task 2.1: domain model constraints and stable ids."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.application.script_authoring.models import (
    GenerationBatch,
    GenerationFingerprint,
    GenerationJob,
    LiveSessionBrief,
    ProductScriptPlan,
    ScriptItem,
    ScriptSegment,
    ScriptSet,
    ScriptState,
    ScriptVersion,
    new_id,
)

SET_ID = "script_set:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ITEM_ID = "script_item:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _segment(index: int, item_id: str = ITEM_ID) -> ScriptSegment:
    return ScriptSegment(
        id=f"segment:{index:032x}",
        script_item_id=item_id,
        plan_id="plan:cccccccccccccccccccccccccccccccc",
        segment_index=index,
        title=f"Segment {index}",
        spoken_text=f"spoke {index}",
    )


def _plan(item_id: str = ITEM_ID) -> ProductScriptPlan:
    return ProductScriptPlan(
        id="plan:cccccccccccccccccccccccccccccccc",
        script_item_id=item_id,
        product_id="P001",
        target_duration_s=600,
        K=2,
        segments=[_segment(0, item_id), _segment(1, item_id)],
    )


# --- stable ids --------------------------------------------------------------


def test_new_id_prefixes_are_stable() -> None:
    assert new_id("script_set").startswith("script_set:")
    assert new_id("approval").startswith("approval:")
    # Two calls never collide (uuid4).
    assert new_id("job") != new_id("job")


# --- ScriptSet ---------------------------------------------------------------


def test_script_set_deduplicates_product_ids_preserving_order() -> None:
    item = ScriptSet(id=SET_ID, shop_id="shop-1", product_ids=["P001", "P002", "P001"])
    assert item.product_ids == ["P001", "P002"]


def test_script_set_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ScriptSet(id=SET_ID, shop_id="shop-1", surprise="x")  # type: ignore[call-arg]


def test_script_set_id_must_match_pattern() -> None:
    with pytest.raises(ValidationError):
        ScriptSet(id="not-an-id", shop_id="shop-1")


def test_brief_defaults_to_order_agnostic() -> None:
    item = ScriptSet(id=SET_ID, shop_id="shop-1")
    assert item.brief.transition_policy == "ORDER_AGNOSTIC"


# --- ScriptItem / ScriptState -------------------------------------------------


def test_item_defaults_to_empty_state() -> None:
    item = ScriptItem(id=ITEM_ID, script_set_id=SET_ID, product_id="P001")
    assert item.state is ScriptState.EMPTY
    assert item.approved_version_id is None


# --- ProductScriptPlan / ScriptSegment ---------------------------------------


def test_plan_requires_exactly_k_segments_aligned() -> None:
    plan = _plan()
    assert plan.segment_count == 2
    assert [s.segment_index for s in plan.segments] == [0, 1]


def test_plan_rejects_misaligned_segment_indices() -> None:
    with pytest.raises(ValidationError, match="segment index"):
        ProductScriptPlan(
            id="plan:cccccccccccccccccccccccccccccccc",
            script_item_id=ITEM_ID,
            product_id="P001",
            target_duration_s=600,
            K=2,
            segments=[_segment(0), _segment(2)],  # 2 != position 1
        )


def test_plan_rejects_zero_segment_count() -> None:
    with pytest.raises(ValidationError):
        ProductScriptPlan(
            id="plan:cccccccccccccccccccccccccccccccc",
            script_item_id=ITEM_ID,
            product_id="P001",
            target_duration_s=600,
            K=0,
            segments=[],
        )


# --- ScriptVersion / text hash -------------------------------------------------


def test_version_text_hash_binds_exact_spoken_text() -> None:
    v1 = ScriptVersion(
        id="script_version:dddddddddddddddddddddddddddddddd",
        script_item_id=ITEM_ID,
        version=1,
        spoken_text="Kem ABC chỉ 299.000đ",
    )
    v2 = ScriptVersion(
        id="script_version:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        script_item_id=ITEM_ID,
        version=2,
        spoken_text="Kem ABC chỉ 299.000 đồng",
    )
    assert v1.text_hash != v2.text_hash
    assert ScriptVersion.compute_text_hash("Kem ABC chỉ 299.000đ") == v1.text_hash


def test_version_rejects_stale_text_hash() -> None:
    with pytest.raises(ValidationError, match="text_hash"):
        ScriptVersion(
            id="script_version:dddddddddddddddddddddddddddddddd",
            script_item_id=ITEM_ID,
            version=1,
            spoken_text="text A",
            text_hash="0" * 64,  # does not match sha256("text A")
        )


def test_version_text_hash_deterministic() -> None:
    assert ScriptVersion.compute_text_hash("abc") == ScriptVersion.compute_text_hash("abc")


# --- GenerationFingerprint / Job / Batch ---------------------------------------


def test_fingerprint_captures_version_deltas() -> None:
    f1 = GenerationFingerprint(skill_version="v1")
    f2 = GenerationFingerprint(skill_version="v2")
    assert f1.skill_version != f2.skill_version
    assert f1.model_dump()["skill_version"] == "v1"


def test_job_tracks_finite_generation_position() -> None:
    job = GenerationJob(
        id="job:ffffffffffffffffffffffffffffffff",
        batch_id="batch:11111111111111111111111111111111",

        script_item_id=ITEM_ID,
        product_id="P001",
        target_duration_s=600,
        plan_segment_count=5,
    )
    assert job.status.value == "queued"
    assert job.current_segment_index == 0
    assert job.attempt_count == 0


def test_batch_aggregates_products_and_jobs() -> None:
    batch = GenerationBatch(
        id="batch:11111111111111111111111111111111",
        script_set_id=SET_ID,
        product_ids=["P001", "P002"],
        job_ids=["job:a", "job:b"],
        estimated_semantic_calls=8,
    )
    assert batch.estimated_semantic_calls == 8
    assert batch.product_ids == ["P001", "P002"]


def test_brief_transition_policy_literal_enforced() -> None:
    with pytest.raises(ValidationError):
        LiveSessionBrief(transition_policy="FLEXIBLE")  # type: ignore[arg-type]
