"""Bounded structured session continuity (task 11.2).

``SessionMemory`` tracks introduced products, recently discussed entities,
active campaign facts, the last spoken topic/product, and unresolved
commitments ("will answer X later") across a session. Every collection is
bounded by the shared deterministic ``EvictionPolicy``; ``render_context()``
returns only the bounded content that goes into model context.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .bounded_memory import EvictionPolicy, MemoryStore, estimate_tokens


@dataclass(frozen=True)
class SessionEntry:
    """One bounded session-memory entry with metadata.

    ``is_commitment`` marks unresolved commitments ("will answer X later")
    so they are evicted only as a last resort once nothing else is
    evictable.
    """

    content: str
    is_commitment: bool = False
    entity_ids: tuple[str, ...] = ()


class SessionMemory(MemoryStore):
    """Bounded structured continuity for one live session.

    All collections are FIFO-bounded by the shared eviction policy, so a
    long stream never grows the store. Budget eviction prefers the oldest
    non-commitment entry; commitments survive while any evictable entry
    exists.
    """

    def __init__(self, policy: EvictionPolicy = EvictionPolicy()) -> None:
        super().__init__(policy)
        self._last_spoken_topic: Optional[str] = None
        self._last_spoken_product_id: Optional[str] = None

    def note_spoken_topic(self, topic: str, product_id: Optional[str] = None) -> None:
        """Record the topic/product just spoken by the host."""
        self._last_spoken_topic = topic
        if product_id is not None:
            self._last_spoken_product_id = product_id

    def add(self, key: str, content: str, *, is_commitment: bool = False) -> None:
        """Insert (or update) a session entry; evict oldest when over budget."""
        self._put(key, SessionEntry(content=content, is_commitment=is_commitment))
        self._enforce_entry_cap()
        self._drop_until_within_budget()

    def get(self, key: str) -> Optional[str]:
        entry = self._get(key)
        return entry.content if isinstance(entry, SessionEntry) else None

    def drop(self, key: str) -> None:
        self._drop(key)

    @property
    def last_spoken_topic(self) -> Optional[str]:
        return self._last_spoken_topic

    @property
    def last_spoken_product_id(self) -> Optional[str]:
        return self._last_spoken_product_id

    def render_context(self) -> dict[str, object]:
        """Bounded dict of session memory for model context."""
        return {
            "entries": {key: entry.content for key, entry in self._entries_by_insertion_order()},
            "last_spoken_topic": self._last_spoken_topic,
            "last_spoken_product_id": self._last_spoken_product_id,
            "tokens": self._tokens(),
        }

    def _entries_by_insertion_order(self) -> list[tuple[str, SessionEntry]]:
        return [
            (key, entry)
            for key, entry in ((key, self._get(key)) for key in self._keys())
            if isinstance(entry, SessionEntry)
        ]

    def _tokens(self) -> int:
        return sum(
            estimate_tokens(entry.content) for _, entry in self._entries_by_insertion_order()
        )

    def _drop_oldest(self) -> None:
        """Drop the oldest non-commitment entry; fall back to the oldest entry.

        Keeps the hard size bound even when every entry is a commitment.
        """
        for key, entry in self._entries_by_insertion_order():
            if not entry.is_commitment:
                self._drop(key)
                return
        self._drop(self._keys()[0])
