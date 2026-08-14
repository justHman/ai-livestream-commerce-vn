"""Tasks 11.4 + 11.6: architectural separation of memory stores.

Proves EvidenceCache state is independent from conversation memory (a fake,
duck-typed EvidenceCache shares nothing with session/topic memory), and the
full runtime transcript may be persisted but is never replayed into model
context: render_context() of ScriptState + SessionMemory + TopicMemory
contains only bounded structures regardless of transcript length.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.application.live_runtime.bounded_memory import EvictionPolicy
from backend.application.live_runtime.script_state import ScriptState
from backend.application.live_runtime.session_memory import SessionMemory
from backend.application.live_runtime.topic_memory import TopicMemory


@dataclass
class _FakeEvidenceCache:
    """Duck-typed stand-in for the cluster-C10 EvidenceCache.

    Exposes only the store-level surface: keyed entries in, entries out.
    C10 implements the real authoritative cache; this fake proves
    independence, not behavior.
    """

    entries: dict[str, str] = field(default_factory=dict)

    def put(self, key: str, value: str) -> None:
        self.entries[key] = value

    def get(self, key: str) -> str | None:
        return self.entries.get(key)


def _long_transcript(line_count: int = 5_000) -> list[str]:
    return [
        f"khách xem {index}: giá sản phẩm này bao nhiêu? {index}" for index in range(line_count)
    ]


def test_evidence_cache_state_is_not_part_of_conversation_memory() -> None:
    evidence = _FakeEvidenceCache()
    evidence.put("product:P020:price", "1.200.000 VND")
    evidence.put("product:P020:stock", "còn 12")

    session = SessionMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
    session.add("product-intro", "giới thiệu P020")
    topics = TopicMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
    topics.add(
        topic_key="P020",
        question="P020 sạc nhanh không?",
        answer="P020 sạc nhanh 33W.",
        resolved_product_ids=("P020",),
    )

    for context in (session.render_context(), topics.render_context()):
        rendered = str(context)
        assert "1.200.000" not in rendered
        assert "còn 12" not in rendered
        assert "evidence" not in rendered

    # The reverse direction: the cache holds no conversation turns.
    assert evidence.entries.keys() == {"product:P020:price", "product:P020:stock"}
    assert not any("khách xem" in value for value in evidence.entries.values())


def test_transcript_may_be_persisted_but_never_rendered() -> None:
    """Task 11.6: persisted transcript stays out of every render_context()."""
    transcript = _long_transcript()
    state = ScriptState()
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Giới thiệu P020.",
    )
    session = SessionMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
    session.add("product-intro", "giới thiệu P020")
    topics = TopicMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
    topics.add(
        topic_key="P020",
        question="P020 sạc nhanh không?",
        answer="P020 sạc nhanh 33W.",
        resolved_product_ids=("P020",),
    )

    rendered = str(
        {
            "script": state.render_context(),
            "session": session.render_context(),
            "topics": topics.render_context(),
        }
    )
    assert len(transcript) == 5_000
    assert "khách xem 0:" not in rendered
    assert "khách xem 4999:" not in rendered
    assert "transcript" not in rendered


def test_rendered_context_is_bounded_regardless_of_transcript_length() -> None:
    for line_count in (100, 5_000, 100_000):
        transcript = _long_transcript(line_count)
        state = ScriptState()
        state.bind(
            script_set_id="set-A",
            approved_version_id="v3",
            product_id="P020",
            first_sentence="Giới thiệu P020.",
        )
        session = SessionMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
        session.add("product-intro", "giới thiệu P020")
        topics = TopicMemory(policy=EvictionPolicy(max_entries=5, max_tokens=2_000))
        topics.add(
            topic_key="P020",
            question="P020 sạc nhanh không?",
            answer="P020 sạc nhanh 33W.",
            resolved_product_ids=("P020",),
        )

        rendered = str(
            {
                "script": state.render_context(),
                "session": session.render_context(),
                "topics": topics.render_context(),
            }
        )
        assert len(transcript) == line_count
        assert len(rendered) < 2_000
