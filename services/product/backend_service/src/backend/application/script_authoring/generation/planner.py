"""One-call long-form product planner with backend-fixed K (tasks 7.5/7.6/7.7).

The model proposes a content plan in a single bounded planning call; the
backend reconciles the proposed sections into the precomputed fixed segment
count K (design Decision 7) without asking the model for additional loops.
The reconciled plan is persisted before prose generation: exactly K ordered
segments, each referencing only authoritative fact/objection IDs.

The planner is deterministic: for the same model plan candidate and
authoritative context it produces the same final ``ProductScriptPlan``, and
any unknown/duplicate/impossible reference is rejected with a deterministic
``PlanRejectionError``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field, field_serializer

from backend.application.script_authoring.generation.calibration import (
    GenerationBudgetCalibration,
    GenerationBudgetError,
)


class PlanRejectionError(ValueError):
    """Deterministic rejection of a planner input (task 7.6)."""


class ProductScriptPlan(BaseModel):
    """Immutable backend-fixed plan for one product script (task 7.5).

    Attributes:
        product_id: Stable product identifier.
        target_duration_s: Requested spoken duration for this product.
        segments: Exactly K ordered segments (``segment_index`` 0..K-1).
    """

    product_id: str = Field(min_length=1)
    target_duration_s: float = Field(gt=0)
    segments: list["PlannedSegment"] = Field(default_factory=list)


class PlannedSegment(BaseModel):
    """One backend-fixed segment assignment (task 7.5).

    Attributes:
        segment_index: Position in the plan, 0..K-1.
        topic: Content topic of this segment.
        intent: Selling intent of this segment.
        target_duration_s: Backend-fixed duration budget for this segment.
        allowed_fact_ids: Authoritative fact IDs this segment may reference.
        allowed_objection_ids: Authoritative objection IDs this segment may
            handle.
        cta_intent: Optional call-to-action intent (``None`` = no CTA).
        transition_intent: Optional transition intent to the next segment.
    """

    segment_index: int = Field(ge=0)
    topic: str = Field(min_length=1)
    intent: str = Field(min_length=1)
    target_duration_s: float = Field(gt=0)
    allowed_fact_ids: frozenset[str] = Field(default_factory=frozenset)
    allowed_objection_ids: frozenset[str] = Field(default_factory=frozenset)
    cta_intent: Optional[str] = None
    transition_intent: Optional[str] = None

    @field_serializer("allowed_fact_ids", "allowed_objection_ids")
    def _serialize_sets(self, v: frozenset[str]) -> list[str]:
        return sorted(v)


@dataclass(frozen=True)
class AuthoritativeContext:
    """Authoritative fact/objection registry supplied by the backend.

    Planner output may reference ONLY these IDs (design Decision 7 "may
    reference only authoritative fact/objection IDs supplied by the
    backend"); anything else is rejected by the planner.
    """

    product_id: str
    fact_ids: frozenset[str]
    objection_ids: frozenset[str]


class ProductScriptPlanner:
    """One-call planner: schema-validate + reconcile into fixed K.

    ``plan`` takes the model's proposed plan candidate plus the
    authoritative context, validates every reference, reconciles the
    proposed sections into exactly K ordered segments (padding with
    synthesized segments when the model proposes fewer, truncating when it
    proposes more), and returns the final immutable plan WITHOUT asking the
    model for another planning loop (task 7.7).
    """

    def __init__(self, calibration: GenerationBudgetCalibration) -> None:
        self._calibration = calibration

    def plan(
        self,
        product_id: str,
        target_duration_s: float,
        authoritative: AuthoritativeContext,
        candidate_sections: list[dict],
    ) -> ProductScriptPlan:
        """Reconcile a model plan candidate into the final fixed-K plan.

        Raises ``PlanRejectionError`` (deterministic) for:
        - a target duration outside the calibration bounds (``GenerationBudgetError``);
        - a fact/objection reference that is not in the authoritative context;
        - a duplicate fact/objection reference inside one segment;
        - an impossible coverage request (a segment whose allowed references
          are all already consumed by earlier segments — see ``_is_covered``).
        """
        if product_id != authoritative.product_id:
            raise PlanRejectionError(
                f"product_id {product_id!r} does not match authoritative "
                f"context product {authoritative.product_id!r}"
            )
        try:
            k = self._calibration.segment_count_for(target_duration_s)
        except GenerationBudgetError as exc:
            raise PlanRejectionError(str(exc)) from exc

        used_facts: set[str] = set()
        used_objections: set[str] = set()
        segments: list[PlannedSegment] = []

        for index in range(k):
            section = candidate_sections[index] if index < len(candidate_sections) else {}
            segment = self._reconcile_segment(
                section=section,
                index=index,
                k=k,
                target_duration_s=target_duration_s,
                authoritative=authoritative,
                used_facts=used_facts,
                used_objections=used_objections,
            )
            segments.append(segment)
            used_facts.update(segment.allowed_fact_ids)
            used_objections.update(segment.allowed_objection_ids)

        return ProductScriptPlan(
            product_id=product_id,
            target_duration_s=target_duration_s,
            segments=segments,
        )

    # -- reconciliation -----------------------------------------------------

    def _reconcile_segment(
        self,
        *,
        section: dict,
        index: int,
        k: int,
        target_duration_s: float,
        authoritative: AuthoritativeContext,
        used_facts: set[str],
        used_objections: set[str],
    ) -> PlannedSegment:
        """Build one fixed-K segment from a proposed section (or synthesize).

        Proposed sections beyond K are dropped (no dynamic expansion);
        missing sections are synthesized with a topic/intent that keeps the
        plan non-repetitive. References are validated before use, and a
        segment whose allowed references are already fully covered by
        earlier segments is rejected as an impossible coverage request.
        """
        facts = self._validated_references(
            section.get("allowed_fact_ids", ()),
            authoritative.fact_ids,
            used_facts,
            kind="fact",
            segment_index=index,
        )
        objections = self._validated_references(
            section.get("allowed_objection_ids", ()),
            authoritative.objection_ids,
            used_objections,
            kind="objection",
            segment_index=index,
        )

        topic = str(section.get("topic") or f"Phần {index + 1}")
        intent = str(section.get("intent") or "bổ sung nội dung")
        return PlannedSegment(
            segment_index=index,
            topic=topic,
            intent=intent,
            target_duration_s=target_duration_s / k,
            allowed_fact_ids=facts,
            allowed_objection_ids=objections,
            cta_intent=section.get("cta_intent"),
            transition_intent=section.get("transition_intent"),
        )

    def _validated_references(
        self,
        raw: object,
        authoritative_ids: frozenset[str],
        used: set[str],
        *,
        kind: str,
        segment_index: int,
    ) -> frozenset[str]:
        """Validate references against authoritative IDs (task 7.6).

        Rejects unknown IDs, duplicate IDs inside one segment, and
        references already used by earlier segments (duplicate coverage).
        """
        if isinstance(raw, str):
            candidates = [raw]
        elif isinstance(raw, (list, tuple, set, frozenset)):
            candidates = list(raw)
        else:
            raise PlanRejectionError(
                f"segment {segment_index} {kind} references must be a list of "
                f"strings, got {type(raw).__name__}"
            )
        ids: list[str] = []
        for value in candidates:
            ref = str(value).strip()
            if not ref:
                continue
            if ref not in authoritative_ids:
                raise PlanRejectionError(
                    f"segment {segment_index} references unknown {kind} "
                    f"{ref!r}: not in the authoritative context"
                )
            if ref in ids:
                raise PlanRejectionError(
                    f"segment {segment_index} duplicates {kind} reference {ref!r}"
                )
            if ref in used:
                raise PlanRejectionError(
                    f"segment {segment_index} re-uses {kind} reference {ref!r} "
                    f"already assigned to an earlier segment"
                )
            ids.append(ref)
        return frozenset(ids)
