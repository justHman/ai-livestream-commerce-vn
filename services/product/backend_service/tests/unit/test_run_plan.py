"""RunPlan schema + coverage + cursor (M3). Offline hashing embedder."""

from __future__ import annotations


from backend.api.v1 import ProductIn, build_run_plan
from backend.application.director.scoring import coverage_ratio, mark_coverage
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.state import DirectorCursor, StreamState
from backend.application.schemas.run_plan import RunPlan
from backend.application.schemas.utterance import AvatarAction, Utterance


def test_build_run_plan_deterministic() -> None:
    products = [
        ProductIn(id="a", name="SP A", features=["cotton", "sale 30%"]),
        ProductIn(id="b", name="SP B", description="siêu nhẹ", price=50000),
    ]
    p1 = build_run_plan(products, persona="MC test")
    p2 = build_run_plan(products, persona="MC test")
    assert p1.model_dump() == p2.model_dump()
    assert p1.phases[0] == "opening"
    assert p1.phases[-1] == "closing"
    assert len(p1.selling) == 2
    assert p1.selling[0].key_selling_points == ["cotton", "sale 30%"]
    assert any("50000" in s or "Giá" in s for s in p1.selling[1].key_selling_points)
    assert p1.opening.persona == "MC test"


def test_utterance_schema() -> None:
    u = Utterance(speech="Xin chào", action=AvatarAction.wave, product_id="p1")
    assert u.action == AvatarAction.wave
    schema = Utterance.json_schema_for_guided()
    assert "speech" in schema["properties"]
    assert "action" in schema["properties"]


def test_mark_coverage_exact_point_with_hash_embedder() -> None:
    emb = HashingEmbedder(dim=64)
    points = ["vải cotton 100%", "freeship toàn quốc"]
    # Identical text → cosine ~1.0 with hashing embedder.
    covered = mark_coverage(emb, "vải cotton 100%", points, threshold=0.75, already_covered=set())
    assert "vải cotton 100%" in covered
    assert coverage_ratio(covered, points) == 0.5


def test_cursor_advance_and_covered_points() -> None:
    st = StreamState()
    assert isinstance(st.cursor, DirectorCursor)
    assert st.cursor.talking_point_idx == 0
    st.advance_talking_point(3)
    assert st.cursor.talking_point_idx == 1
    st.advance_talking_point(3)
    st.advance_talking_point(3)
    assert st.cursor.talking_point_idx == 2  # clamped
    st.mark_product_covered("p1", {"a", "b"})
    st.mark_product_covered("p1", {"b", "c"})
    assert st.covered_points["p1"] == {"a", "b", "c"}


def test_run_plan_roundtrip_json() -> None:
    plan = build_run_plan([ProductIn(id="x", name="X", features=["f1"])], persona=None)
    raw = plan.model_dump()
    restored = RunPlan.model_validate(raw)
    assert restored.selling[0].product_id == "x"
