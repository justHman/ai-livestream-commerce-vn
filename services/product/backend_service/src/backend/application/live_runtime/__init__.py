"""Live-runtime agent memory layer (OpenSpec change multi-platform-agentic-live-director).

Owns the bounded structured memory that replaces an ever-growing LLM chat
transcript as the model context (design Decisions 15-16; tasks 11.1-11.7):

- ``ScriptPosition``/``ScriptState``: authoritative live script position
  (script set/version, product, sentence index, last completed sentence,
  exact next sentence). This package models and bounds the state only —
  sentence-map derivation and cursor advancement are owned by the
  speech-arbiter cluster (``script_cursor.py``, cluster C13).
- ``SessionMemory``: bounded structured continuity (introduced products,
  recent entities, campaign facts, last spoken topic/product, unresolved
  commitments).
- ``TopicMemory``: bounded keyed recent Q&A turns for referential follow-up
  resolution ("vậy cái đó...?" -> last answered topic/entity).
- ``EvictionPolicy`` + ``estimate_tokens``: deterministic eviction and token
  budget shared by every bounded collection.

All memory stores render a bounded dict via ``render_context()``; the full
runtime transcript stays diagnostic data and is never replayed into model
context. The authoritative EvidenceCache lives in its own store, independent
of conversation turns (cluster C10).
"""

from __future__ import annotations

from .bounded_memory import EvictionPolicy, MemoryStore, estimate_tokens
from .script_state import ScriptPosition, ScriptState
from .session_memory import SessionMemory
from .topic_memory import TopicMemory, resolve_reference

__all__ = [
    "EvictionPolicy",
    "MemoryStore",
    "ScriptPosition",
    "ScriptState",
    "SessionMemory",
    "TopicMemory",
    "estimate_tokens",
    "resolve_reference",
]
