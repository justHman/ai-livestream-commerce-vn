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
from .evidence_prefetch import EvidencePrefetcher, PrefetchConfig, VolatileRevalidator
from .hard_interrupt import HARD_INTERRUPT_OPERATION, HardInterruptService, is_hard_interrupt
from .resume_bridge import DEFAULT_RESUME_BRIDGE_TEMPLATE, build_resume_bridge, should_speak_bridge
from .pending_qa import PendingQaCandidate, PendingQaStore, QaHysteresisConfig
from .qa_resolver import (
    BoundaryQaResolver,
    QaResolution,
    QaResolutionService,
    VolatileEvidenceSource,
)
from .transitions import (
    DEFAULT_QA_LEAD_IN_TEMPLATE,
    INTENT_TOPIC_PHRASES,
    build_qa_lead_in,
)
from .script_state import ScriptPosition, ScriptState
from .session_memory import SessionMemory
from .speech_arbiter import ArbiterConfig, ArbiterState, SpeechArbiter, SpeechTextLike
from .topic_memory import TopicMemory, resolve_reference

__all__ = [
    "ArbiterConfig",
    "ArbiterState",
    "BoundaryQaResolver",
    "EvictionPolicy",
    "MemoryStore",
    "PendingQaCandidate",
    "PendingQaStore",
    "QaHysteresisConfig",
    "QaResolution",
    "QaResolutionService",
    "ScriptPosition",
    "ScriptState",
    "SessionMemory",
    "SpeechArbiter",
    "SpeechTextLike",
    "TopicMemory",
    "DEFAULT_QA_LEAD_IN_TEMPLATE",
    "DEFAULT_RESUME_BRIDGE_TEMPLATE",
    "EvidencePrefetcher",
    "HARD_INTERRUPT_OPERATION",
    "HardInterruptService",
    "PrefetchConfig",
    "VolatileEvidenceSource",
    "VolatileRevalidator",
    "INTENT_TOPIC_PHRASES",
    "build_qa_lead_in",
    "build_resume_bridge",
    "estimate_tokens",
    "is_hard_interrupt",
    "resolve_reference",
    "should_speak_bridge",
]
