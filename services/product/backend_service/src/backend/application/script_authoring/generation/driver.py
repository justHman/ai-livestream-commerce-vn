"""Step-based adapter over ``ProductGenerationWorkflow`` for the batch driver.

The batch orchestrator (``generation/batch.py``) drives a
``ProductGenerationWorkflow`` protocol: ``step() / is_terminal() /
snapshot() / restore()``. The FSM dataclass does not implement that surface;
this adapter does, for the AI long-form path only (manual drafts never enter
the batch). ``step()`` advances exactly one finite backend-owned phase and the
adapter persists artifacts (plan / segment / version / gate run) through an
injected ``persist`` sink; recovery rehydrates from the persisted rows (via
injected sync loaders) without re-running completed segments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from backend.application.script_authoring.generation.continuity import (
    ContinuityState,
    build_tail,
    closing_fingerprint,
    extract_ctas,
)
from backend.application.script_authoring.generation.segment_generator import (
    SegmentGenerationResult,
    SegmentStepOutcome,
)
from backend.application.script_authoring.models import (
    GenerationFingerprint,
    ProductScriptPlan,
    ScriptItem,
    ScriptSegment,
    ScriptState,
    ScriptVersion,
    new_id,
)
from backend.application.script_authoring.workflow import ProductGenerationWorkflow

# One bounded semantic call that produces the planned section list + fixed K.
PlanOutcome = Callable[[], tuple[int, list[dict]]]
# One bounded semantic call per preplanned segment index; the third arg is
# the segment's planned target duration (``target_duration_s / K``) so the
# prompt can state a realistic per-segment length budget.
SegmentGenerate = Callable[[int, ContinuityState, Optional[float]], SegmentStepOutcome]


@dataclass
class SegmentRepairHint:
    """Constrained inputs for an in-place segment repair (reviewer R9.2/3.3).

    The exact failed candidate text plus the exact failed rule IDs/messages, so
    the repair prompt asks the model to make the minimum local change instead of
    blindly rewriting the segment from scratch. ``target_duration_s`` is the
    segment's planned spoken-duration target so a duration repair knows how much
    content to write (reviewer R9.6 — the 15.4 repair under-produced without it).
    """

    source_text: str
    failed_rule_ids: list[str]
    repair_instructions: list[str]
    target_duration_s: Optional[float] = None


# One bounded semantic call that repairs a gate-failed segment IN PLACE with
# constrained inputs (failed candidate + failed rules). Consumes the same fixed
# per-segment attempt budget as a blind regeneration.
SegmentRepair = Callable[[int, ContinuityState, SegmentRepairHint], SegmentStepOutcome]
# Sink for non-item artifacts (plan / segment / version / gate run).
PersistSink = Callable[[Any], None]
# Sync loaders used by restore() (callers bridge async repositories).
ItemLoader = Callable[[str], ScriptItem]
SegmentLoader = Callable[[str], ScriptSegment]
VersionLoader = Callable[[str], ScriptVersion]

_TERMINAL = {
    ScriptState.REVIEWABLE,
    ScriptState.GATE_FAILED,
    ScriptState.FAILED,
    ScriptState.CANCELLED,
}


@dataclass
class WorkflowDriver:
    """Finite per-product AI long-form driver over the FSM (batch protocol)."""

    product_id: str
    workflow: ProductGenerationWorkflow
    plan_generate: PlanOutcome
    segment_generate: SegmentGenerate
    persist: PersistSink
    load_item: ItemLoader
    load_segment: SegmentLoader
    load_version: VersionLoader
    plan_version: int = 1
    fingerprint: Optional[GenerationFingerprint] = None
    # Bounded cross-segment context carried between segment generations so the
    # next prompt sees the previous tail + covered claims/opening fingerprints
    # and avoids REPETITION_CROSS (15.4 real-E2E finding: an always-empty
    # ContinuityState let every segment restate the same price/SKU/claims).
    _continuity: ContinuityState = field(default_factory=ContinuityState)
    # Segment in-place retry bound (15.4 money optimization): a real LLM fails
    # a segment gate 1-2 words at a time; regenerating the WHOLE script throws
    # away the passing segments and costs K+1 calls per retry. Instead a
    # segment is regenerated in place up to this many attempts, keeping prior
    # passing segments + continuity. Only after all attempts fail does the
    # segment land GATE_FAILED (human fix path). ``max_segment_attempts`` is
    # the TOTAL semantic attempts per segment, including the initial generation
    # (reviewer R9.2: planned = 1+K, maximum = 1+K*max_segment_attempts).
    max_segment_attempts: int = 3
    # Optional constrained in-place repair callable used for attempts 2..N
    # (reviewer R9.2/3.3). When set, a retry hands the exact failed candidate +
    # failed rule IDs/messages to a constrained repair prompt so the model makes
    # the minimum local change instead of blind regeneration. When None, retry
    # attempts regenerate the segment blindly (still inside the fixed budget).
    segment_repair: Optional[SegmentRepair] = None

    def is_terminal(self) -> bool:
        return self.workflow.item.state in _TERMINAL

    def step(self) -> bool:
        """Advance exactly one finite phase; False at terminal."""
        wf = self.workflow
        state = wf.item.state

        if state is ScriptState.EMPTY:
            if wf.target_duration_s is None:
                raise ValueError("target_duration_s must be set before start")
            wf.start_planning(target_duration_s=wf.target_duration_s)
            return True

        if state is ScriptState.PLANNING:
            self._complete_planning()
            return True

        if state is ScriptState.GENERATING:
            index = len(wf.segments)
            if wf.plan_segment_count is None:
                raise ValueError("plan_segment_count not set; complete_planning first")
            if index < wf.plan_segment_count:
                self._generate_segment(index)
                return True
            self._compile_and_gate()
            return True

        # Terminal states have no further work.
        return False

    def _complete_planning(self) -> None:
        wf = self.workflow
        segment_count, candidates = self.plan_generate()
        wf.complete_planning(segment_count=segment_count)
        plan = ProductScriptPlan(
            id=wf.plan_id or new_id("plan"),
            script_item_id=wf.item.id,
            version=self.plan_version,
            product_id=wf.item.product_id,
            target_duration_s=wf.target_duration_s or 0,
            K=segment_count,
            segments=[
                ScriptSegment(
                    id=new_id("segment"),
                    script_item_id=wf.item.id,
                    plan_id=wf.plan_id or "",
                    segment_index=idx,
                    title=str(c.get("title", "")),
                    intent=str(c.get("intent", "")),
                    target_duration_s=int(c.get("target_duration_s", 0) or 0),
                    status=ScriptState.DRAFT,
                )
                for idx, c in enumerate(candidates[:segment_count])
            ],
            fingerprint=wf.item.id,
        )
        self.persist(plan)

    def _generate_segment(self, index: int) -> None:
        wf = self.workflow
        wf.start_segment(index)
        continuity = self._continuity
        # Deterministic per-segment target, mirroring the segment gate's band
        # (Decision 7). None when no plan -> prompt falls back to the
        # product-level target.
        segment_target = (
            wf.target_duration_s / wf.plan_segment_count
            if wf.target_duration_s is not None and wf.plan_segment_count
            else None
        )
        # Bounded, auditable, cost-visible Segment Repair inside one user-level
        # Generate operation (reviewer R9.2 / 2026-08-21 product correction):
        #   attempt 1      = normal segment generation
        #   attempt 2..N   = constrained segment-local repair/regeneration
        # with ``max_segment_attempts`` the TOTAL semantic attempts per segment
        # (N includes the initial generation). One semantic candidate -> ONE
        # Segment Gate evaluation -> one immutable candidate row + one GateRun
        # (``record_segment_attempt``); a failed attempt stays persisted as
        # audit evidence, prior passing sibling segments are kept, no N+1 work
        # happens until N passes or exhausts its budget, and an exhausted
        # budget lands truthful GATE_FAILED (no fresh whole-script restart is
        # hidden inside Generate).
        attempts = max(1, self.max_segment_attempts)
        selected: Optional[SegmentGenerationResult] = None
        last_failure: Optional[tuple[SegmentGenerationResult, object]] = None
        for attempt in range(1, attempts + 1):
            # Attempt 1 = normal generation; attempts 2..N = constrained
            # segment-local repair when the violation is known (reviewer
            # R9.2/3.3), else blind regeneration of the same index.
            if attempt > 1 and last_failure is not None and self.segment_repair is not None:
                prev_result, prev_gate = last_failure
                hint = SegmentRepairHint(
                    source_text=prev_result.spoken_text,
                    failed_rule_ids=[v.rule_id for v in prev_gate.violations],
                    repair_instructions=[v.message for v in prev_gate.violations],
                    target_duration_s=segment_target,
                )
                outcome = self.segment_repair(index, continuity, hint)
            else:
                outcome = self.segment_generate(index, continuity, segment_target)
            if outcome.error is not None:
                wf.segment_failed(index, message=str(outcome.error))
                self.persist(wf.item)
                return
            result = outcome.result
            if result is None:
                wf.segment_failed(index, message="segment generator returned no result")
                self.persist(wf.item)
                return
            # ONE deterministic Segment Gate evaluation per semantic candidate
            # (reviewer R9.2/3.4): never gate the same candidate twice.
            gate = wf.segment_gate(result.spoken_text, segment_target)
            # Persist the immutable candidate + its GateRun as auditable attempt
            # evidence — a failed candidate before the final attempt must not
            # disappear even though LLM money was spent on it.
            candidate = wf.record_segment_attempt(
                index,
                display_text=result.display_text,
                spoken_text=result.spoken_text,
                attempt=attempt,
                gate_result=gate,
            )
            self.persist(candidate)
            if wf.last_gate_run is not None:
                self.persist(wf.last_gate_run)
            if gate.passed:
                selected = result
                break
            # gate failed: budget remaining -> constrain-repair/regenerate THIS
            # index in place with the exact failed rules as context.
            last_failure = (result, gate)
        if selected is None:
            wf.fail_segment_gate(
                index,
                message=f"segment {index} gate failed after {attempts} semantic attempts",
            )
            self.persist(wf.item)
            return
        # Advance the bounded cross-segment state so the next prompt avoids the
        # previous tail / covered claims / used opening fingerprints (15.4).
        # used_ctas + closing_fingerprints let the prompt list EVERY already-used
        # CTA/closing so a real LLM cannot repeat one across non-adjacent
        # segments (segments 1,3,5 of a K=5 script, 15.4 real-LLM E2E finding).
        self._continuity = ContinuityState(
            previous_segment_tail=build_tail(selected.spoken_text),
            covered_fact_ids=continuity.covered_fact_ids
            | (selected.covered_fact_ids or frozenset()),
            opening_fingerprints=continuity.opening_fingerprints
            | ({selected.opening_fingerprint} if selected.opening_fingerprint else frozenset()),
            used_ctas=continuity.used_ctas | extract_ctas(selected.spoken_text),
            closing_fingerprints=continuity.closing_fingerprints
            | ({fp for fp in (closing_fingerprint(selected.spoken_text),) if fp}),
            cta_count=continuity.cta_count + (1 if selected.cta_used else 0),
            last_topic=selected.topic or continuity.last_topic,
            next_topic=None,
        )

    def _compile_and_gate(self) -> None:
        wf = self.workflow
        wf.compile_and_full_gate()
        if wf.current_version is not None:
            self.persist(wf.current_version)
        if wf.last_gate_run is not None:
            self.persist(wf.last_gate_run)

    # ── persistence / recovery ──────────────────────────────────────────

    def snapshot(self) -> dict:
        wf = self.workflow
        return {
            "product_id": self.product_id,
            "item_id": wf.item.id,
            "state": wf.item.state.value,
            "current_version_id": wf.item.current_version_id,
            "approved_version_id": wf.item.approved_version_id,
            "plan_id": wf.plan_id,
            "plan_segment_count": wf.plan_segment_count,
            "target_duration_s": wf.target_duration_s,
            "semantic_calls": wf.semantic_calls,
            "segment_versions": [
                {"index": idx, "id": seg.id, "status": seg.status.value}
                for idx, seg in sorted(wf.segments.items())
            ],
            "version_ids": [v.id for v in wf.versions],
        }

    def restore(self, state: dict) -> None:
        """Rehydrate the FSM from persisted rows; never re-runs completed work.

        Uses the injected sync loaders to fetch the full item/segments/versions
        by id, then replays only the finite counters from the snapshot.
        """
        wf = self.workflow
        wf.item = self.load_item(state["item_id"])
        wf.item.state = ScriptState(state["state"])
        wf.item.current_version_id = state.get("current_version_id")
        wf.item.approved_version_id = state.get("approved_version_id")
        wf.plan_id = state.get("plan_id")
        wf.plan_segment_count = state.get("plan_segment_count")
        wf.target_duration_s = state.get("target_duration_s")
        wf.semantic_calls = int(state.get("semantic_calls", 0))
        for entry in state.get("segment_versions", []):
            segment = self.load_segment(entry["id"])
            wf.segments[int(entry["index"])] = segment
        for version_id in state.get("version_ids", []):
            version = self.load_version(version_id)
            wf.versions.append(version)
            wf.current_version = version
