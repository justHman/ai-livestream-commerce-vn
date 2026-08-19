"""Typed domain models for pre-live script authoring (tasks 2.1-2.3).

Pure pydantic v2 / stdlib value objects: no network, LLM, or filesystem
access. The aggregate root is ``ScriptSet`` (Decision 1), which is
independent of any runtime session. Versions are immutable once persisted
(Decision 13); repository layers are responsible for enforcing that.

Every model carries a stable string id (``<type>:<uuid>``) so persisted
rows, API payloads, SSE events, and fingerprints can reference the same
identity without depending on BIGSERIAL integers.
"""

from __future__ import annotations

import hashlib
import uuid
from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ScriptState",
    "ScriptSource",
    "ScriptIntent",
    "ScriptSet",
    "ScriptItem",
    "ProductScriptPlan",
    "ScriptSegment",
    "ScriptVersion",
    "GateRun",
    "GateViolation",
    "Approval",
    "GenerationFingerprint",
    "GenerationBatch",
    "GenerationJob",
    "new_id",
]

# TransitionPolicy lives in ``gate/context.py`` (task 3.x owns it); this
# module re-exports the canonical definition so ``models`` stays the single
# import point for authoring-domain concepts.
from backend.application.script_authoring.gate.context import TransitionPolicy


def new_id(prefix: str) -> str:
    """Return a stable unique id like ``<prefix>:<uuid4-hex>``."""
    return f"{prefix}:{uuid.uuid4().hex}"


def _hash_digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


class ScriptState(StrEnum):
    """Legal states of a ``ScriptItem`` (Decision 2 diagram, exactly).

    Generation/cancel/failure substates are explicit enum members so a
    restarting worker can reconstruct the finite workflow position without
    reading model prose (Decision 20).
    """

    EMPTY = "empty"
    DRAFT = "draft"
    PLANNING = "planning"
    GENERATING = "generating"
    GATE_RUNNING = "gate_running"
    GATE_FAILED = "gate_failed"
    AI_FIXING = "ai_fixing"
    REVIEWABLE = "reviewable"
    APPROVED = "approved"
    STALE = "stale"
    CANCELLED = "cancelled"
    FAILED = "failed"


class ScriptSource(StrEnum):
    """How the current draft content came to exist."""

    MANUAL = "manual"
    AI_GENERATE = "ai_generate"
    AI_FIX = "ai_fix"
    AI_REGENERATE = "ai_regenerate"


class ScriptIntent(StrEnum):
    """Authoring intent for one product script (Decision 7)."""

    GENERATE_LONG_FORM = "generate_long_form"
    GENERATE_SEGMENT = "generate_segment"
    FIX_FAILED = "fix_failed"
    REGENERATE_SEGMENT = "regenerate_segment"
    MANUAL_DRAFT = "manual_draft"


class GenerationJobStatus(StrEnum):
    """Persisted finite generation-job states (Decision 20)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GenerationBatchStatus(StrEnum):
    """Aggregate batch state (Decision 10)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    CANCELLED = "cancelled"
    FAILED = "failed"


class LiveSessionBrief(BaseModel):
    """Authoring-level brief for the planned livestream (Decision 1).

    Not a runtime session — this is the pre-live intent record.
    """

    title: str = ""
    persona: str = ""
    transition_policy: TransitionPolicy = "ORDER_AGNOSTIC"
    shop_name: str = ""
    notes: str = ""


class ScriptSet(BaseModel):
    """Aggregate root for one planned livestream's product scripts."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^script_set:[0-9a-f]{32}$")
    shop_id: str = Field(min_length=1)
    title: str = ""
    brief: LiveSessionBrief = Field(default_factory=LiveSessionBrief)
    product_ids: list[str] = Field(default_factory=list)
    # Immutable once bound; the session-binding command (task 12.x) owns this.
    session_id: Optional[str] = None
    revision: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=lambda: _now_iso())
    updated_at: str = Field(default_factory=lambda: _now_iso())

    @field_validator("product_ids")
    @classmethod
    def _dedupe_products(cls, v: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for pid in v:
            if pid not in seen:
                seen.add(pid)
                ordered.append(pid)
        return ordered


class ScriptItem(BaseModel):
    """Per-product workflow state within a ScriptSet (Decision 1/2).

    ``current_version_id`` / ``approved_version_id`` are pointer columns
    that change as new immutable versions are created; they never mutate a
    historical version row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^script_item:[0-9a-f]{32}$")
    script_set_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    state: ScriptState = ScriptState.EMPTY
    source: Optional[ScriptSource] = None
    current_version_id: Optional[str] = None
    approved_version_id: Optional[str] = None
    intent: Optional[ScriptIntent] = None
    revision: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=lambda: _now_iso())
    updated_at: str = Field(default_factory=lambda: _now_iso())


class ProductScriptPlan(BaseModel):
    """Immutable structured plan: fixed K segments (Decision 7)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^plan:[0-9a-f]{32}$")
    script_item_id: str = Field(min_length=1)
    version: int = Field(default=1, ge=1)
    product_id: str = Field(min_length=1)
    target_duration_s: int = Field(ge=1)
    segment_count: int = Field(alias="K", ge=1)
    # Exactly ``segment_count`` entries, index-aligned with segment order.
    segments: list[ScriptSegment] = Field(default_factory=list)
    fingerprint: str = Field(default="")
    created_at: str = Field(default_factory=lambda: _now_iso())

    @field_validator("segments")
    @classmethod
    def _align_segments(cls, v: list[ScriptSegment]) -> list[ScriptSegment]:
        for idx, segment in enumerate(v):
            if segment.segment_index != idx:
                raise ValueError(f"segment index {segment.segment_index} != position {idx}")
        return v


class ScriptSegment(BaseModel):
    """One immutable plan/segment version within a product (Decision 7)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^segment:[0-9a-f]{32}$")
    script_item_id: str = Field(min_length=1)
    plan_id: str = Field(min_length=1)
    segment_index: int = Field(ge=0)
    title: str = ""
    intent: str = ""
    target_duration_s: int = Field(default=0, ge=0)
    display_text: str = ""
    spoken_text: str = ""
    status: ScriptState = ScriptState.DRAFT
    version: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=lambda: _now_iso())


class ScriptVersion(BaseModel):
    """Immutable compiled script version (Decision 13).

    ``text_hash`` binds the exact spoken artifact; any text change produces
    a different hash and therefore a different version row.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^script_version:[0-9a-f]{32}$")
    script_item_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    state: ScriptState = ScriptState.DRAFT
    source: ScriptSource = ScriptSource.MANUAL
    display_text: str = ""
    spoken_text: str = ""
    # Derives from ``spoken_text`` (Decision 4/14); keep fields consistent.
    text_hash: Optional[str] = None
    segment_version_ids: list[str] = Field(default_factory=list)
    plan_version: int = Field(default=1, ge=1)
    gate_run_id: Optional[str] = None
    fingerprint: Optional[GenerationFingerprint] = None
    created_at: str = Field(default_factory=lambda: _now_iso())

    @classmethod
    def compute_text_hash(cls, spoken_text: str) -> str:
        """Deterministic sha256 over the exact spoken text (Decision 4/14)."""
        return _hash_digest(spoken_text)

    def model_post_init(self, __context) -> None:
        """Fill the hash from ``spoken_text`` when omitted; never diverge."""
        expected = _hash_digest(self.spoken_text or "")
        if self.text_hash is None:
            self.text_hash = expected
        elif self.text_hash != expected:
            raise ValueError("text_hash must equal sha256(spoken_text)")


class GateViolation(BaseModel):
    """One deterministic rule violation from a gate run (task 3.2)."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(min_length=1)
    severity: str = Field(pattern=r"^(error|warning)$")
    message: str = ""
    segment_index: Optional[int] = Field(default=None, ge=0)
    span_start: Optional[int] = Field(default=None, ge=0)
    span_end: Optional[int] = Field(default=None, ge=0)


class GateRun(BaseModel):
    """One deterministic gate execution (task 3.2)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^gate_run:[0-9a-f]{32}$")
    script_item_id: str = Field(min_length=1)
    # True = full-script gate over the compiled version; False = segment gate.
    full: bool = True
    passed: bool = False
    violations: list[GateViolation] = Field(default_factory=list)
    rule_set_fingerprint: str = Field(default="")
    script_version_id: Optional[str] = None
    created_at: str = Field(default_factory=lambda: _now_iso())


class Approval(BaseModel):
    """Immutable human approval record (Decision 14)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^approval:[0-9a-f]{32}$")
    script_item_id: str = Field(min_length=1)
    script_version_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    approval_hash: str = Field(min_length=1)
    gate_run_id: str = Field(min_length=1)
    created_at: str = Field(default_factory=lambda: _now_iso())


class GenerationFingerprint(BaseModel):
    """Reproducibility metadata without chain-of-thought (Decision 13)."""

    model_config = ConfigDict(extra="forbid")

    model: str = ""
    skill_version: str = ""
    rule_set_version: str = ""
    prompt_template_version: str = ""
    product_facts_version: str = ""
    promotion_version: str = ""
    persona_brief_version: str = ""
    plan_version: int = Field(default=1, ge=1)
    generation_params: dict[str, str] = Field(default_factory=dict)


class GenerationJob(BaseModel):
    """One finite generation job (Decision 12/20)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^job:[0-9a-f]{32}$")
    batch_id: str = Field(min_length=1)
    script_item_id: str = Field(min_length=1)
    product_id: str = Field(min_length=1)
    intent: ScriptIntent = ScriptIntent.GENERATE_LONG_FORM
    status: GenerationJobStatus = GenerationJobStatus.QUEUED
    plan_id: Optional[str] = None
    plan_segment_count: Optional[int] = Field(default=None, ge=1)
    current_segment_index: int = Field(default=0, ge=0)
    attempt_count: int = Field(default=0, ge=0)
    target_duration_s: int = Field(ge=1)
    fingerprint: Optional[GenerationFingerprint] = None
    idempotency_key: str = ""
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    lease_epoch: int = Field(default=0, ge=0)
    created_at: str = Field(default_factory=lambda: _now_iso())
    updated_at: str = Field(default_factory=lambda: _now_iso())


class GenerationBatch(BaseModel):
    """Multi-product batch of per-product jobs (Decision 10)."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, pattern=r"^batch:[0-9a-f]{32}$")
    script_set_id: str = Field(min_length=1)
    status: GenerationBatchStatus = GenerationBatchStatus.QUEUED
    product_ids: list[str] = Field(default_factory=list)
    job_ids: list[str] = Field(default_factory=list)
    estimated_semantic_calls: int = Field(default=0, ge=0)
    idempotency_key: str = ""
    lease_owner: Optional[str] = None
    lease_expires_at: Optional[str] = None
    lease_epoch: int = Field(default=0, ge=0)
    # Durable cross-replica cancel request (R8.4): any replica sets this; only
    # the execution owner consumes it. The batch row is the source of truth.
    cancel_requested: bool = False
    created_at: str = Field(default_factory=lambda: _now_iso())
    updated_at: str = Field(default_factory=lambda: _now_iso())


def _now_iso() -> str:
    """ISO-8601 UTC timestamp for new-domain-model defaults."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
