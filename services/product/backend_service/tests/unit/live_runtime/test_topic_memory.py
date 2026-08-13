"""Tasks 11.3 + 11.7: bounded keyed recent Q&A turns (TopicMemory).

Proves: re-asking about the same topic updates the same entry (no growth);
bounded by the eviction policy with deterministic eviction; and the 11.7
fixture — prior answered topic P020, then "vậy cái đó có sạc nhanh không?"
resolves to P020 via bounded memory only, with no transcript involved.
"""

from __future__ import annotations

from backend.application.live_runtime.bounded_memory import EvictionPolicy
from backend.application.live_runtime.topic_memory import TopicMemory, resolve_reference


def _seed_p020(memory: TopicMemory) -> None:
    memory.add(
        topic_key="P020",
        question="P020 sạc nhanh không?",
        answer="P020 hỗ trợ sạc nhanh 33W, đầy pin trong 40 phút.",
        entity_ids=("product:P020",),
        resolved_product_ids=("P020",),
        spoken_topic="sạc nhanh",
    )


def test_keyed_update_does_not_grow() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=10, max_tokens=10_000))
    _seed_p020(memory)
    for index in range(100):
        memory.add(
            topic_key="P020",
            question=f"P020 sạc nhanh không? (lần {index})",
            answer="P020 hỗ trợ sạc nhanh 33W.",
            entity_ids=("product:P020",),
            resolved_product_ids=("P020",),
        )
    assert memory.size == 1
    turn = memory.get("P020")
    assert turn is not None
    assert turn.question == "P020 sạc nhanh không? (lần 99)"


def test_bounded_by_entries_with_deterministic_eviction() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=3, max_tokens=10_000))
    for product_id in ("P001", "P002", "P003", "P004"):
        memory.add(
            topic_key=product_id,
            question=f"{product_id} giá bao nhiêu?",
            answer=f"{product_id} giá 1 triệu.",
            resolved_product_ids=(product_id,),
        )
    assert memory.size == 3
    assert memory.get("P001") is None
    assert all(memory.get(product_id) is not None for product_id in ("P002", "P003", "P004"))


def test_bounded_by_token_budget() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=1_000, max_tokens=100))
    for index in range(100):
        memory.add(
            topic_key=f"P{index:03d}",
            question=f"Sản phẩm {index} có sạc nhanh không? " * 5,
            answer=f"Sản phẩm {index} hỗ trợ sạc nhanh 33W. " * 5,
            resolved_product_ids=(f"P{index:03d}",),
        )
    assert memory.size <= 1_000
    assert memory._tokens() <= 100


def test_last_topic_is_most_recently_answered() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=10, max_tokens=10_000))
    memory.add(
        topic_key="P001",
        question="P001 giá bao nhiêu?",
        answer="P001 giá 1 triệu.",
        resolved_product_ids=("P001",),
    )
    memory.add(
        topic_key="P020",
        question="P020 sạc nhanh không?",
        answer="P020 sạc nhanh 33W.",
        resolved_product_ids=("P020",),
    )
    assert memory.last_topic_key() == "P020"
    assert memory.last_turn() is not None
    assert memory.last_turn().resolved_product_ids == ("P020",)


def test_reanswering_existing_topic_refreshes_recency() -> None:
    """Re-answering P001 makes IT the last answered topic, not P020."""
    memory = TopicMemory(policy=EvictionPolicy(max_entries=10, max_tokens=10_000))
    memory.add(
        topic_key="P001",
        question="P001 giá bao nhiêu?",
        answer="P001 giá 1 triệu.",
        resolved_product_ids=("P001",),
    )
    memory.add(
        topic_key="P020",
        question="P020 sạc nhanh không?",
        answer="P020 sạc nhanh 33W.",
        resolved_product_ids=("P020",),
    )
    memory.add(
        topic_key="P001",
        question="P001 màu gì?",
        answer="P001 màu đen.",
        resolved_product_ids=("P001",),
    )

    assert memory.last_topic_key() == "P001"
    turn = resolve_reference("vậy cái đó có sạc nhanh không?", memory)
    assert turn is not None
    assert "P001" in turn.resolved_product_ids


def test_resolve_reference_maps_to_last_answered_topic() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=10, max_tokens=10_000))
    memory.add(
        topic_key="P001",
        question="P001 giá bao nhiêu?",
        answer="P001 giá 1 triệu.",
        resolved_product_ids=("P001",),
    )
    _seed_p020(memory)

    turn = resolve_reference("vậy cái đó có sạc nhanh không?", memory)
    assert turn is not None
    assert "P020" in turn.resolved_product_ids


def test_resolve_reference_without_topic_returns_none() -> None:
    memory = TopicMemory()
    assert resolve_reference("vậy cái đó có sạc nhanh không?", memory) is None


def test_resolve_reference_without_referential_head_returns_none() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=10, max_tokens=10_000))
    _seed_p020(memory)
    assert resolve_reference("P021 giá bao nhiêu?", memory) is None


def test_fixture_117_resolves_p020_without_transcript() -> None:
    """Spec scenario 11.7: follow-up resolves via bounded memory only.

    The fixture contains NO transcript object at all — resolution cannot
    have consulted one.
    """
    memory = TopicMemory(policy=EvictionPolicy(max_entries=10, max_tokens=2_000))
    _seed_p020(memory)

    turn = resolve_reference("vậy cái đó có sạc nhanh không?", memory)
    assert turn is not None
    assert "P020" in turn.resolved_product_ids
    assert turn.spoken_topic == "sạc nhanh"


def test_render_context_is_bounded_and_keyed() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
    _seed_p020(memory)
    memory.add(
        topic_key="P001",
        question="P001 giá bao nhiêu?",
        answer="P001 giá 1 triệu.",
        resolved_product_ids=("P001",),
    )

    context = memory.render_context()
    assert set(context["turns"].keys()) == {"P020", "P001"}
    assert context["last_topic_key"] == "P001"
    assert "transcript" not in context
    assert context["tokens"] <= 2_000


def test_render_context_excludes_unbounded_content() -> None:
    memory = TopicMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
    transcript = [
        "khách xem 1: giá bao nhiêu?",
        "host: giá 1,2 triệu.",
        "khách xem 2: cái đó sạc nhanh không?",
    ]
    _seed_p020(memory)

    rendered = str(memory.render_context())
    for line in transcript:
        assert line not in rendered
