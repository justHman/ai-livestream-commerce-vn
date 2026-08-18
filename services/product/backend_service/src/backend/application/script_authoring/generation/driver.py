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

from dataclasses import dataclass
from typing import Any, Callable, Optional

from backend.application.script_authoring.generation.continuity import ContinuityState
from backend.application.script_authoring.generation.segment_generator import (
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
# One bounded semantic call per preplanned segment index.
SegmentGenerate = Callable[[int, ContinuityState], SegmentStepOutcome]
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
        continuity = ContinuityState()
        outcome = self.segment_generate(index, continuity)
        if outcome.error is not None:
            wf.segment_failed(index, message=str(outcome.error))
            self.persist(wf.item)
            return
        result = outcome.result
        if result is None:
            wf.segment_failed(index, message="segment generator returned no result")
            self.persist(wf.item)
            return
        gate = wf.complete_segment(
            index,
            display_text=result.display_text,
            spoken_text=result.spoken_text,
        )
        # Persist the exact selected segment version (content + status) and its
        # segment gate run; the FSM persists the item itself via its sink.
        segment = wf.segments[index]
        segment.status = ScriptState.GATE_FAILED if gate.passed is False else ScriptState.DRAFT
        self.persist(segment)
        if wf.last_gate_run is not None:
            self.persist(wf.last_gate_run)

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
