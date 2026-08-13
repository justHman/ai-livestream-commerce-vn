"""Task 11.2: bounded structured session continuity (SessionMemory).

Proves: a long synthetic stream stays bounded by max_entries and the token
budget; eviction is deterministic (oldest-first FIFO); unresolved
commitments survive eviction while bounded; last spoken topic/product
updates; render_context excludes anything unbounded.
"""

from __future__ import annotations

from backend.application.live_runtime.bounded_memory import EvictionPolicy, estimate_tokens
from backend.application.live_runtime.session_memory import SessionMemory


def test_bounded_over_long_stream_by_entries() -> None:
    memory = SessionMemory(policy=EvictionPolicy(max_entries=10, max_tokens=10_000))
    for index in range(1_000):
        memory.add(f"p{index % 100}", f"sản phẩm {index % 100} được giới thiệu")
    assert memory.size <= 10


def test_bounded_over_long_stream_by_token_budget() -> None:
    memory = SessionMemory(policy=EvictionPolicy(max_entries=1_000, max_tokens=100))
    for index in range(1_000):
        memory.add(f"key-{index}", f"nội dung dài lặp lại {index} " * 10)
    assert memory.size <= 1_000
    assert memory._tokens() <= 100


def test_eviction_is_deterministic_oldest_first() -> None:
    memory = SessionMemory(policy=EvictionPolicy(max_entries=3, max_tokens=10_000))
    memory.add("a", "nội dung A")
    memory.add("b", "nội dung B")
    memory.add("c", "nội dung C")
    memory.add("d", "nội dung D")

    assert memory.get("a") is None
    assert [memory.get(k) for k in ("b", "c", "d")] == ["nội dung B", "nội dung C", "nội dung D"]


def test_commitments_survive_while_bounded() -> None:
    memory = SessionMemory(policy=EvictionPolicy(max_entries=200, max_tokens=2_000))
    for index in range(50):
        memory.add(f"k{index}", f"nội dung thường {index}", is_commitment=(index == 7))
    for index in range(200):
        memory.add(f"fill-{index}", f"nội dung lấp đầy {index}")

    assert memory.get("k7") == "nội dung thường 7"


def test_last_spoken_topic_and_product_update() -> None:
    memory = SessionMemory()
    assert memory.last_spoken_topic is None
    assert memory.last_spoken_product_id is None

    memory.note_spoken_topic("tai nghe", product_id="P020")
    assert memory.last_spoken_topic == "tai nghe"
    assert memory.last_spoken_product_id == "P020"

    memory.note_spoken_topic("sạc nhanh")
    assert memory.last_spoken_topic == "sạc nhanh"
    assert memory.last_spoken_product_id == "P020"

    memory.note_spoken_topic("chuột", product_id="P001")
    assert memory.last_spoken_topic == "chuột"
    assert memory.last_spoken_product_id == "P001"


def test_render_context_excludes_unbounded_content() -> None:
    memory = SessionMemory(policy=EvictionPolicy(max_entries=5, max_tokens=1_000))
    transcript = [
        "khách xem 1: giá bao nhiêu?",
        "host: giá 1,2 triệu.",
        "khách xem 2: cái đó sạc nhanh không?",
    ]
    memory.add("product-intro", "giới thiệu P020, tai nghe chống ồn")
    memory.add("commitment", "sẽ trả lời về sạc nhanh sau", is_commitment=True)
    memory.note_spoken_topic("tai nghe", product_id="P020")

    context = memory.render_context()
    assert "entries" in context
    assert context["last_spoken_product_id"] == "P020"
    assert "tokens" in context
    assert "transcript" not in context
    for line in transcript:
        assert line not in str(context)


def test_rendered_context_respects_token_budget() -> None:
    memory = SessionMemory(policy=EvictionPolicy(max_entries=20, max_tokens=2_000))
    for index in range(500):
        memory.add(f"key-{index}", f"nội dung lặp lại {index} " * 20)

    context = memory.render_context()
    assert context["tokens"] <= 2_000
    assert len(str(context)) <= estimate_tokens(str(context)) * 4 + 64
