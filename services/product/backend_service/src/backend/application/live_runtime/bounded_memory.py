"""Shared bounded-memory policy for the live-runtime stores (task 11.5).

Deterministic eviction and token budget: every bounded collection evicts
least-recently-updated first (inserts and keyed updates refresh recency)
and enforces ``max_entries`` and ``max_tokens`` so a session stays bounded
regardless of stream length.
"""

from __future__ import annotations

from dataclasses import dataclass

# Token-estimation heuristic: one token ~= 4 characters (chars/4).
# Canonical estimate reused from script_authoring/generation/prompt_builder.py;
# deterministic and dependency-free (documented as an estimate).
_CHARS_PER_TOKEN: int = 4


def estimate_tokens(text: str) -> int:
    """Rough token count for budget guarding (chars/4 heuristic).

    Documented as an estimate: the model's real tokenizer may differ by a
    constant factor, which the explicit budget covers.
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass(frozen=True)
class EvictionPolicy:
    """Deterministic eviction and token-budget policy for bounded memory.

    The entry limit keeps the store structurally bounded; the token budget
    keeps its rendered context within the model-context allowance. Eviction
    order is deterministic: least-recently-updated entry first — inserting
    a new key appends it, updating an existing key refreshes its recency
    (moves it to the back).
    """

    max_entries: int = 20
    max_tokens: int = 2000


class _Bounded:
    """Shared FIFO bounded store mechanics over a keyed recency order."""

    def __init__(self, policy: EvictionPolicy) -> None:
        self._policy = policy
        self._items: dict[str, object] = {}
        self._order: list[str] = []

    def _put(self, key: str, item: object) -> None:
        if key not in self._items:
            self._order.append(key)
        else:
            self._order.remove(key)
            self._order.append(key)
        self._items[key] = item

    def _get(self, key: str) -> object | None:
        return self._items.get(key)

    def _drop(self, key: str) -> None:
        if key in self._items:
            self._order.remove(key)
            del self._items[key]

    def _keys(self) -> list[str]:
        return list(self._order)

    def _size(self) -> int:
        return len(self._items)

    def _tokens(self) -> int:
        return sum(estimate_tokens(str(item)) for item in self._items.values())

    def _over_budget(self) -> bool:
        return self._tokens() > self._policy.max_tokens

    def _drop_oldest(self) -> None:
        self._items.pop(self._order.pop(0))


class MemoryStore:
    """Bounded keyed memory with deterministic FIFO eviction.

    Public basis of the session/topic stores: shared eviction mechanics over
    the same ``EvictionPolicy``; subclasses expose their own typed entry
    types and helpers. Not safe for concurrent mutation — a live runtime
    owns each store per session (or locks it externally).
    """

    def __init__(self, policy: EvictionPolicy) -> None:
        self._b = _Bounded(policy)

    @property
    def policy(self) -> EvictionPolicy:
        return self._b._policy

    @property
    def size(self) -> int:
        return self._b._size()

    def _put(self, key: str, item: object) -> None:
        self._b._put(key, item)

    def _get(self, key: str) -> object | None:
        return self._b._get(key)

    def _drop(self, key: str) -> None:
        self._b._drop(key)

    def _keys(self) -> list[str]:
        return self._b._keys()

    def _tokens(self) -> int:
        return self._b._tokens()

    def is_within_budget(self) -> bool:
        """Whether the store's token total fits the policy's token budget."""
        return self._tokens() <= self.policy.max_tokens

    def _drop_oldest(self) -> None:
        self._b._drop_oldest()

    def _drop_until_within_budget(self) -> None:
        while self._b._size() > 0 and self._b._over_budget():
            self._drop_oldest()

    def _enforce_entry_cap(self) -> None:
        """Evict oldest entries until ``max_entries`` holds.

        Delegates to ``self._drop_oldest()`` so subclasses keep their
        eviction preferences (e.g. commitments-first protection).
        """
        while self._b._size() > self._b._policy.max_entries:
            self._drop_oldest()
