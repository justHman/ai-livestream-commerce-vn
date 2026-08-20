"""Finite product-generation workflow: plan -> K segments -> full gate (9.1-9.6).

Implements design Decision 2's gate-first, AI-optional state machine as a
single in-memory FSM with injected boundaries:

- ``generate``      - one bounded semantic call (planning or segment prose);
- ``segment_gate``  - deterministic Segment Gate over one segment's text;
- ``full_gate``     - deterministic Full Script Gate over the compiled script;
- ``persist``       - durability hook invoked after every persisted change.

The workflow owns control flow only (design Decision 6): the model can never
advance state, retry, or increase ``K``. Gate PASS never approves (Decision
2/14); only an explicit human ``approve`` lands in ``APPROVED``. An AI Fix
always produces a NEW draft version that must be explicitly submitted and
gated again (Decision 5 / spec: "the result SHALL still be a new DRAFT and
ScriptGate SHALL run again").

Manual drafts bypass AI entirely: ``create_manual_draft`` -> ``submit`` ->
gate -> ``REVIEWABLE`` with zero ``generate`` calls when compliant (Decision
2 diagram, task 9.4).

Shared-state edges: the canonical Decision-2 table (``state.py``) covers the
manual DRAFT/GATE_RUNNING/REVIEWABLE/APPROVED cycle and the AI entry
transitions. Decision 9's generation-substate edges (segment gate FAIL and
full gate PASS/FAIL while GENERATING) are not in that summary table; they are
validated explicitly here and persisted through the same ``persist`` hook.
Segment PASS keeps the workflow in GENERATING (sequential loop); only
``compile_and_full_gate`` moves it out of GENERATING.

This module is pure: no network, no LLM imports. All semantic work arrives
through the injected ``generate`` callable, and all gate work through the
injected gate callables (``script_authoring`` never imports
``speech_chunking`` or ``render.windows.TextChunk``).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Optional

from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleViolation,
)
from backend.application.script_authoring.models import (
    GateRun,
    GateViolation,
    ScriptIntent,
    ScriptItem,
    ScriptSegment,
    ScriptSource,
    ScriptState,
    ScriptVersion,
    new_id,
)
from backend.application.script_authoring.state import (
    IllegalTransitionError,
    TransitionVerb,
    transition,
)

__all__ = [
    "ProductGenerationWorkflow",
    "InvalidFixStateError",
    "SegmentText",
    "CompiledScript",
    "SegmentGateCallable",
    "FullGateCallable",
    "GenerateCallable",
    "PersistCallable",
]

#: A segment's accepted content shape. Compatible with ``SegmentGenerationResult``
#: (display_text/spoken_text) and with manual segment text.
SegmentText = str

#: One bounded semantic call (planning or segment/repair prose). The
#: orchestrator binds product/plan/continuity context into the closure; the
#: workflow itself never calls the model directly.
GenerateCallable = Callable[..., object]


@dataclass(frozen=True)
class CompiledScript:
    """Exact compiled product script (Decision 7/9.2).

    ``segment_texts`` is the exact ordered list of the SELECTED segment
    versions; the full gate runs over exactly this list, and
    ``spoken_text``/``display_text`` are its deterministic joins.
    """

    segment_texts: tuple[SegmentText, ...]
    display_text: str
    spoken_text: str


# --- injected boundaries ------------------------------------------------------

#: Segment gate receives the checked text plus the segment's planned target
#: duration (``target_duration_s / K``) when the AI long-form plan is active;
#: ``None`` means no plan (manual draft) so the gate falls back to the lenient
#: default band. One-shot LLM variance means the band is derived from the
#: target (see service_impl), never a fixed 10-180s window that a planned
#: 200-240s segment cannot satisfy.
SegmentGateCallable = Callable[[SegmentText, Optional[float]], GateRunResult]
FullGateCallable = Callable[[Sequence[str]], GateRunResult]
PersistCallable = Callable[[ScriptItem], None]


class InvalidFixStateError(ValueError):
    """AI Fix requested from a state that is not eligible (task 9.5).

    The API layer maps this to HTTP 409 with the stable
    ``fix_not_eligible`` domain code (see ``service.ScriptAuthoringError``).
    """

    def __init__(self, state: ScriptState) -> None:
        self.state = state
        super().__init__(f"AI fix requires GATE_FAILED, got state {state.value!r}")


# --- workflow -----------------------------------------------------------------


@dataclass
class ProductGenerationWorkflow:
    """Finite per-product authoring FSM (tasks 9.1-9.7).

    State lives in ``item.state``; every transition is validated against the
    canonical Decision 2/9 rules and then persisted via the injected
    ``persist`` callable. Immutable version rows are appended for every
    content-producing action so history is auditable and approval binds the
    exact version (Decision 13/14).

    Attributes:
        item: The ``ScriptItem`` being driven (its ``state`` is the FSM).
        segment_gate: Deterministic segment-scope gate.
        full_gate: Deterministic full-script-scope gate.
        persist: Called with the item after every persisted change.
        generate: Optional bounded semantic callable used by the AI paths.
            ``None`` disables AI paths (manual-only workflow, zero LLM calls).
        versions: Immutable compiled-version history (newest last).
        current_version: The current compiled ``ScriptVersion``, if any.
        segments: Selected segment versions keyed by index (Decision 9.2:
            compile uses EXACTLY these, never a newer sibling).
        plan_id / plan_segment_count: Backend-fixed plan identity and K.
        target_duration_s: Requested spoken duration (observability, D21).
        last_gate_run: The most recent gate run (segment or full scope).
        last_compile: The last compiled script (None until compile ran).
        semantic_calls: Count of injected ``generate`` calls made.
        last_error: Last recorded failure reason (recovery/telemetry).
    """

    item: ScriptItem
    segment_gate: SegmentGateCallable
    full_gate: FullGateCallable
    persist: PersistCallable
    generate: Optional[GenerateCallable] = None
    versions: list[ScriptVersion] = field(default_factory=list)
    current_version: Optional[ScriptVersion] = None
    segments: dict[int, ScriptSegment] = field(default_factory=dict)
    plan_id: Optional[str] = None
    plan_segment_count: Optional[int] = None
    target_duration_s: Optional[int] = None
    last_gate_run: Optional[GateRun] = None
    last_compile: Optional[CompiledScript] = None
    semantic_calls: int = 0
    last_error: Optional[str] = None

    # -- helpers ---------------------------------------------------------

    def _persist_transition(self, verb: TransitionVerb) -> None:
        """Validate + apply + persist one canonical shared-table transition."""
        self.item.state = transition(self.item.state, verb)
        self.persist(self.item)

    def _set_state(self, state: ScriptState) -> None:
        """Assign and persist a workflow-owned state (Decision 9 edges)."""
        self.item.state = state
        self.persist(self.item)

    def _append_version(self, version: ScriptVersion) -> None:
        self.versions.append(version)
        self.current_version = version

    def _requires_ai(self) -> None:
        if self.generate is None:
            raise ValueError("workflow has no generate callable; AI paths are disabled")

    # -- task 9.1: state entry points ------------------------------------

    def start_planning(self, *, target_duration_s: int) -> None:
        """Begin the AI long-form path from EMPTY (Decision 7).

        Persisted transition: EMPTY -> PLANNING. The backend-fixed K is
        persisted at ``complete_planning``; no model call happens here.
        """
        if self.item.state is not ScriptState.EMPTY:
            raise IllegalTransitionError(self.item.state, "generate")
        self._requires_ai()
        self.item.intent = ScriptIntent.GENERATE_LONG_FORM
        self.target_duration_s = target_duration_s
        self._persist_transition("generate")

    def complete_planning(self, *, segment_count: int) -> None:
        """Accept the backend-fixed plan: persist K, move to GENERATING.

        ``segment_count`` is the hard workflow bound (Decision 7): the model
        cannot increase it. Persisted transition: PLANNING -> GENERATING.
        """
        if self.item.state is not ScriptState.PLANNING:
            raise IllegalTransitionError(self.item.state, "plan_ready")
        if segment_count < 1:
            raise ValueError("plan segment_count must be >= 1")
        self.plan_segment_count = segment_count
        self.plan_id = new_id("plan")
        self._persist_transition("plan_ready")

    def start_segment(self, index: int) -> None:
        """Begin generation of segment ``index`` (0 <= index < K).

        The current segment must be the next unresolved index after the
        last persisted one, so recovery resumes exactly where the finite
        workflow stopped (Decision 20).
        """
        if self.item.state is not ScriptState.GENERATING:
            raise IllegalTransitionError(self.item.state, "generation_complete")
        if self.plan_segment_count is None:
            raise ValueError("plan segment_count not set; complete_planning first")
        if index < 0 or index >= self.plan_segment_count:
            raise ValueError(
                f"segment index {index} out of fixed bounds 0..{self.plan_segment_count - 1}"
            )
        next_expected = len(self.segments)
        if index != next_expected:
            raise ValueError(
                f"segments are sequential: expected index {next_expected}, got {index}"
            )

    def complete_segment(
        self,
        index: int,
        *,
        display_text: str,
        spoken_text: str,
    ) -> GateRunResult:
        """Persist segment ``index``'s content and run the Segment Gate.

        Returns the segment gate result. On PASS the segment is selected
        and the workflow STAYS in GENERATING for the next index. On
        ERROR-level FAIL the workflow goes GATE_FAILED and later segments
        are never generated (Decision 9, task 9.3): no automatic semantic
        retry, no repair, no spend on N+1..K-1.
        """
        if self.item.state is not ScriptState.GENERATING:
            raise IllegalTransitionError(self.item.state, "generation_complete")
        if display_text == "" or spoken_text == "":
            raise ValueError("segment content must be non-empty")
        if self.plan_id is None:
            raise ValueError("plan not set; complete_planning first")

        segment_version = ScriptSegment(
            id=new_id("segment"),
            script_item_id=self.item.id,
            plan_id=self.plan_id,
            segment_index=index,
            display_text=display_text,
            spoken_text=spoken_text,
            status=ScriptState.GENERATING,
            version=len(self.segments) + 1,
        )
        self.segments[index] = segment_version
        # Planned per-segment target (Decision 7): the planner assigns every
        # segment ``target_duration_s / K`` (see generation/planner.py), so the
        # gate's target range is derived from that same deterministic value.
        # ``None`` when no plan exists (manual draft path) -> lenient default.
        segment_target: Optional[float] = None
        if self.target_duration_s is not None and self.plan_segment_count:
            segment_target = self.target_duration_s / self.plan_segment_count
        result = self.segment_gate(spoken_text, segment_target)

        if result.passed:
            segment_version.status = ScriptState.DRAFT
            self.persist(self.item)  # sync point: segment selected, still GENERATING
            return result

        # Segment gate FAIL: stop this product at N (Decision 9) and land in
        # GATE_FAILED so manual edit / Fix with AI remain legal. Workflow-owned
        # edge (the shared Decision-2 table does not model generation gates).
        segment_version.status = ScriptState.GATE_FAILED
        self._record_gate_run(
            full=False,
            result=result,
            script_version_id=segment_version.id,
        )
        self._set_state(ScriptState.GATE_FAILED)
        return result

    def record_segment_attempt(
        self,
        index: int,
        *,
        display_text: str,
        spoken_text: str,
        attempt: int,
        gate_result: GateRunResult,
    ) -> ScriptSegment:
        """Persist ONE segment candidate evaluated by the Segment Gate exactly once.

        Reviewer R9.2/3.4: one semantic candidate -> one Segment Gate
        evaluation -> one immutable candidate row + one GateRun bound to it.
        ``attempt`` is the 1-based semantic-attempt number within this segment
        index and becomes the candidate's immutable ``version`` (so the
        ``(plan_id, segment_index, version)`` unique key doubles as attempt
        metadata). A passed candidate becomes the SELECTED segment (status
        DRAFT) and the workflow stays in GENERATING for the next index; a
        failed candidate is persisted as GATE_FAILED evidence WITHOUT being
        selected and WITHOUT advancing the item — the driver owns the bounded
        in-place retry and the terminal GATE_FAILED transition via
        ``fail_segment_gate``. ``complete_segment`` remains for the single-shot
        (non-retry) caller.
        """
        if self.item.state is not ScriptState.GENERATING:
            raise IllegalTransitionError(self.item.state, "generation_complete")
        if display_text == "" or spoken_text == "":
            raise ValueError("segment content must be non-empty")
        if self.plan_id is None:
            raise ValueError("plan not set; complete_planning first")
        candidate = ScriptSegment(
            id=new_id("segment"),
            script_item_id=self.item.id,
            plan_id=self.plan_id,
            segment_index=index,
            display_text=display_text,
            spoken_text=spoken_text,
            status=ScriptState.DRAFT if gate_result.passed else ScriptState.GATE_FAILED,
            version=attempt,
        )
        if gate_result.passed:
            self.segments[index] = candidate
        self._record_gate_run(
            full=False,
            result=gate_result,
            script_version_id=candidate.id,
        )
        return candidate

    def fail_segment_gate(self, index: int, *, message: str = "") -> None:
        """Move GENERATING -> GATE_FAILED after the bounded segment auto-heal
        budget is exhausted (reviewer R9.2/3.4).

        All failed candidates stay persisted as immutable audit evidence; later
        segments are never generated (no N+1 work before N resolves). The
        caller (the generation driver) selects which attempt budget was
        exhausted; the workflow never retries or spends on its own.
        """
        if self.item.state is not ScriptState.GENERATING:
            raise IllegalTransitionError(self.item.state, "generation_failed")
        if index != len(self.segments):
            raise ValueError(f"failed segment index {index} != current {len(self.segments)}")
        self.last_error = message
        self._set_state(ScriptState.GATE_FAILED)

    def segment_failed(self, index: int, *, message: str = "") -> None:
        """Mark segment generation as failed (provider/schema error).

        Persisted transition: GENERATING -> FAILED (terminal). No semantic
        retry is performed; a human command starts a fresh workflow.
        """
        if self.item.state is not ScriptState.GENERATING:
            raise IllegalTransitionError(self.item.state, "generation_failed")
        if index != len(self.segments):
            raise ValueError(f"failed segment index {index} != current {len(self.segments)}")
        self.last_error = message
        self._persist_transition("generation_failed")

    def compile_and_full_gate(self) -> GateRunResult:
        """Compile the exact selected segments and run the Full Script Gate.

        Only runs after every required segment passed its local gate
        (task 9.2). On PASS the workflow becomes REVIEWABLE (gate PASS is
        never approval — Decision 2); on FAIL it becomes GATE_FAILED with
        the violations mapped to actionable global/segment violations
        (task 9.3). No automatic semantic retry in either case.
        """
        if self.item.state is not ScriptState.GENERATING:
            raise IllegalTransitionError(self.item.state, "gate_pass")
        if self.plan_segment_count is None:
            raise ValueError("plan segment_count not set; complete_planning first")
        if len(self.segments) != self.plan_segment_count:
            raise ValueError(
                f"cannot compile: {len(self.segments)}/{self.plan_segment_count} "
                "segments passed; full gate runs only after ALL segments pass"
            )

        ordered = [self.segments[i] for i in range(self.plan_segment_count)]
        display_text = "\n\n".join(seg.display_text for seg in ordered)
        spoken_text = "\n\n".join(seg.spoken_text for seg in ordered)
        self.last_compile = CompiledScript(
            segment_texts=tuple(seg.spoken_text for seg in ordered),
            display_text=display_text,
            spoken_text=spoken_text,
        )
        result = self.full_gate(self.last_compile.segment_texts)

        if result.passed:
            self._record_gate_run(full=True, result=result)
            version = ScriptVersion(
                id=new_id("script_version"),
                script_item_id=self.item.id,
                version=len(self.versions) + 1,
                state=ScriptState.REVIEWABLE,
                source=ScriptSource.AI_GENERATE,
                display_text=display_text,
                spoken_text=spoken_text,
                segment_version_ids=[seg.id for seg in ordered],
                gate_run_id=self.last_gate_run.id if self.last_gate_run else None,
            )
            self._append_version(version)
            self.item.current_version_id = version.id
            # Workflow-owned Decision 9 edge: full gate PASS -> REVIEWABLE.
            self._set_state(ScriptState.REVIEWABLE)
            return result

        # Full gate FAIL -> GATE_FAILED with actionable violations (9.3).
        self._record_gate_run(full=True, result=result)
        # Reviewer R9.2/3.7: persist a COMPLETE immutable compiled ScriptVersion
        # even when the Full Script Gate fails, and bind the exact full-gate
        # violations to it (via ``gate_run_id``). The item's current version
        # then IS the failed compiled script, so a later human-triggered
        # ``Fix with AI`` operates on the exact failed artifact instead of an
        # out-of-band reconstruction. A failed compiled version is NOT
        # REVIEWABLE/APPROVED — it is GATE_FAILED and needs an explicit human
        # full-script Fix to become a new draft.
        version = ScriptVersion(
            id=new_id("script_version"),
            script_item_id=self.item.id,
            version=len(self.versions) + 1,
            state=ScriptState.GATE_FAILED,
            source=ScriptSource.AI_GENERATE,
            display_text=display_text,
            spoken_text=spoken_text,
            segment_version_ids=[seg.id for seg in ordered],
            gate_run_id=self.last_gate_run.id if self.last_gate_run else None,
        )
        self._append_version(version)
        self.item.current_version_id = version.id
        self._set_state(ScriptState.GATE_FAILED)
        return result

    # -- task 9.4: manual zero-LLM path -----------------------------------

    def create_manual_draft(
        self,
        *,
        display_text: str,
        spoken_text: str,
    ) -> None:
        """Create a manual DRAFT with zero semantic calls (task 9.4).

        Legal from EMPTY, DRAFT (edit), GATE_FAILED (manual edit),
        REVIEWABLE/APPROVED (content-invalidating edit creates a new draft
        — Decision 2) and STALE. Each call appends a new immutable version;
        history is never mutated.
        """
        allowed = (
            ScriptState.EMPTY,
            ScriptState.DRAFT,
            ScriptState.GATE_FAILED,
            ScriptState.REVIEWABLE,
            ScriptState.APPROVED,
            ScriptState.STALE,
        )
        if self.item.state not in allowed:
            raise IllegalTransitionError(self.item.state, "manual_edit")
        if display_text == "" or spoken_text == "":
            raise ValueError("draft content must be non-empty")
        if self.current_version is not None:
            self.item.approved_version_id = None
        self._persist_transition("manual_edit")
        version = ScriptVersion(
            id=new_id("script_version"),
            script_item_id=self.item.id,
            version=len(self.versions) + 1,
            state=ScriptState.DRAFT,
            source=ScriptSource.MANUAL,
            display_text=display_text,
            spoken_text=spoken_text,
        )
        self._append_version(version)
        self.item.current_version_id = version.id

    def submit(self) -> GateRunResult:
        """Submit the current draft to the gate (task 9.4).

        DRAFT -> GATE_RUNNING; on PASS -> REVIEWABLE, on FAIL -> GATE_FAILED.
        Zero semantic calls; the injected ``full_gate`` is the only consumer
        of the draft.
        """
        if self.item.state is not ScriptState.DRAFT:
            raise IllegalTransitionError(self.item.state, "submit")
        if self.current_version is None:
            raise ValueError("no draft version to submit")
        self._persist_transition("submit")
        result = self.full_gate([self.current_version.spoken_text])
        if result.passed:
            self.current_version.state = ScriptState.REVIEWABLE
            self._record_gate_run(full=True, result=result)
            self.item.current_version_id = self.current_version.id
            self._persist_transition("gate_pass")
        else:
            self.current_version.state = ScriptState.GATE_FAILED
            self._record_gate_run(full=True, result=result)
            self._persist_transition("gate_fail")
        return result

    def make_reviewable(self) -> None:
        """Enter REVIEWABLE after a passed gate, from GATE_RUNNING.

        Persists the canonical ``gate_pass`` transition (GATE_RUNNING ->
        REVIEWABLE). Requires a passed ``last_gate_run``.
        """
        if self.item.state is not ScriptState.GATE_RUNNING:
            raise IllegalTransitionError(self.item.state, "gate_pass")
        if self.last_gate_run is None or not self.last_gate_run.passed:
            raise ValueError("cannot become reviewable without a passed gate run")
        if self.current_version is None:
            raise ValueError("no draft version to make reviewable")
        self.current_version.state = ScriptState.REVIEWABLE
        self._persist_transition("gate_pass")

    # -- task 9.5/9.6: Fix with AI ----------------------------------------

    def fix_eligible(self) -> bool:
        """True only on GATE_FAILED; else raise ``InvalidFixStateError``.

        Fix eligibility is the ONLY AI entry point into a gate-failed
        version (task 9.5); the API maps ``InvalidFixStateError`` to 409.
        """
        if self.item.state is ScriptState.GATE_FAILED:
            return True
        raise InvalidFixStateError(self.item.state)

    def apply_ai_fix(self) -> None:
        """Apply a constrained AI repair to a GATE_FAILED draft (9.5/9.6).

        Makes exactly one bounded ``generate`` call (Decision 5/12: no
        retry loop, no sales skill — the injected closure owns prompt
        construction) and creates a NEW DRAFT version from its
        ``display_text``/``spoken_text``. The result is never auto-submitted
        or auto-approved (spec: "the result SHALL still be a new DRAFT and
        ScriptGate SHALL run again"). A provider failure lands the workflow
        in FAILED (persisted) before re-raising.
        """
        self._requires_ai()
        self.fix_eligible()
        self._persist_transition("ai_fix")  # GATE_FAILED -> AI_FIXING
        self.semantic_calls += 1
        try:
            fixed = self.generate()
        except Exception:
            self._persist_transition("generation_failed")  # AI_FIXING -> FAILED
            raise
        display = getattr(fixed, "display_text", None)
        spoken = getattr(fixed, "spoken_text", None)
        if not display or not spoken:
            self._persist_transition("generation_failed")
            raise ValueError("AI fix result must carry non-empty display_text and spoken_text")
        version = ScriptVersion(
            id=new_id("script_version"),
            script_item_id=self.item.id,
            version=len(self.versions) + 1,
            state=ScriptState.DRAFT,
            source=ScriptSource.AI_FIX,
            display_text=display,
            spoken_text=spoken,
        )
        self._append_version(version)
        self.item.current_version_id = version.id
        self._persist_transition("ai_fix_complete")  # AI_FIXING -> DRAFT

    # -- human approval ----------------------------------------------------

    def approve(self, *, actor: str) -> None:
        """Human-only approval of the exact current version (Decision 14).

        Persisted transition: REVIEWABLE -> APPROVED. Gate PASS never
        reaches here automatically; the only legal path is this explicit
        authenticated command.
        """
        if self.item.state is not ScriptState.REVIEWABLE:
            raise IllegalTransitionError(self.item.state, "approve")
        if self.current_version is None:
            raise ValueError("no reviewable version to approve")
        self.current_version.state = ScriptState.APPROVED
        self.item.approved_version_id = self.current_version.id
        self._persist_transition("approve")

    # -- gate-run recording -------------------------------------------------

    def _record_gate_run(
        self,
        *,
        full: bool,
        result: GateRunResult,
        script_version_id: Optional[str] = None,
    ) -> GateRun:
        """Map a ``GateRunResult`` into the persisted ``GateRun`` row."""
        run = GateRun(
            id=new_id("gate_run"),
            script_item_id=self.item.id,
            full=full,
            passed=result.passed,
            violations=[_to_gate_violation(v) for v in result.violations],
            rule_set_fingerprint=result.fingerprint.hexdigest,
            script_version_id=script_version_id,
        )
        self.last_gate_run = run
        return run

    # -- recovery ----------------------------------------------------------

    @property
    def current_segment_index(self) -> int:
        """Next unresolved segment index (recovery resume point, Decision 20)."""
        return len(self.segments)


def _to_gate_violation(v: RuleViolation) -> GateViolation:
    """Map a gate rule violation to the persisted model (task 9.3).

    ``segment_index`` is carried through when the full-script rule
    attributed it to a specific segment; ``span_start``/``span_end`` come
    from the optional ``TextSpan``.
    """
    return GateViolation(
        rule_id=v.rule_id,
        severity=v.severity.value,
        message=v.message,
        segment_index=v.segment_index,
        span_start=v.text_span.start if v.text_span else None,
        span_end=v.text_span.end if v.text_span else None,
    )
