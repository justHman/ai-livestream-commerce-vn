"""Bounded keyed recent Q&A turns (task 11.3) and referential resolution (11.7).

``TopicMemory`` stores one entry per stable topic key (product or entity id):
re-asking about the same topic updates the same entry, so a stream of
follow-ups never grows the store. ``resolve_reference()`` resolves short
referential follow-ups ("vậy cái đó có sạc nhanh không?") to the most
recently answered topic/entity via bounded memory only — no transcript, no
NLP pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .bounded_memory import EvictionPolicy, MemoryStore, estimate_tokens


@dataclass(frozen=True)
class TopicTurn:
    """One answered Q&A turn keyed by a stable topic key.

    ``entity_ids`` / ``resolved_product_ids`` are the reference metadata
    used to map a follow-up back to a product or entity.
    """

    question: str
    answer: str
    entity_ids: tuple[str, ...] = ()
    resolved_product_ids: tuple[str, ...] = ()
    spoken_topic: Optional[str] = None


class TopicMemory(MemoryStore):
    """Bounded keyed recent Q&A turns for one live session.

    Keyed by topic key (e.g. a product id): re-asking about the same topic
    updates the existing entry instead of growing the store. Bounded by the
    shared deterministic ``EvictionPolicy`` (oldest-first FIFO).
    """

    def __init__(self, policy: EvictionPolicy = EvictionPolicy()) -> None:
        super().__init__(policy)

    def add(
        self,
        *,
        topic_key: str,
        question: str,
        answer: str,
        entity_ids: tuple[str, ...] = (),
        resolved_product_ids: tuple[str, ...] = (),
        spoken_topic: Optional[str] = None,
    ) -> None:
        """Insert (or update, keyed by ``topic_key``) an answered Q&A turn."""
        self._put(
            topic_key,
            TopicTurn(
                question=question,
                answer=answer,
                entity_ids=entity_ids,
                resolved_product_ids=resolved_product_ids,
                spoken_topic=spoken_topic,
            ),
        )
        self._enforce_entry_cap()
        self._drop_until_within_budget()

    def get(self, topic_key: str) -> Optional[TopicTurn]:
        turn = self._get(topic_key)
        return turn if isinstance(turn, TopicTurn) else None

    def last_topic_key(self) -> Optional[str]:
        """The most recently answered topic key (by insertion order)."""
        return self._keys()[-1] if self._keys() else None

    def last_turn(self) -> Optional[TopicTurn]:
        key = self.last_topic_key()
        return self.get(key) if key is not None else None

    def render_context(self) -> dict[str, object]:
        """Bounded dict of recent Q&A turns for model context."""
        return {
            "turns": {
                key: {
                    "question": turn.question,
                    "answer": turn.answer,
                    "entity_ids": list(turn.entity_ids),
                    "resolved_product_ids": list(turn.resolved_product_ids),
                    "spoken_topic": turn.spoken_topic,
                }
                for key, turn in self._turns_by_insertion_order()
            },
            "last_topic_key": self.last_topic_key(),
            "tokens": self._tokens(),
        }

    def _turns_by_insertion_order(self) -> list[tuple[str, TopicTurn]]:
        return [
            (key, turn)
            for key, turn in ((key, self._get(key)) for key in self._keys())
            if isinstance(turn, TopicTurn)
        ]

    def _tokens(self) -> int:
        return sum(
            estimate_tokens(turn.question) + estimate_tokens(turn.answer)
            for _, turn in self._turns_by_insertion_order()
        )


def resolve_reference(text: str, memory: TopicMemory) -> Optional[TopicTurn]:
    """Resolve a referential follow-up via the most recent answered turn.

    Matches Vietnamese referential starters ("cái đó", "vậy cái đó", ...);
    anything else is out of scope for this deterministic, dependency-free
    helper (a richer resolver would replace this function). Returns None
    when no topic has been answered yet or the text carries no reference.
    """
    if not text.strip():
        return None
    head = text.strip().lower().split()[0] if text.strip().split() else ""
    if head not in {"cái", "vậy", "đó", "nó", "còn"}:
        return None
    return memory.last_turn()
