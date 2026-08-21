"""Task 9.7: table-driven ProductGenerationWorkflow tests.

Proves the Decision 2/9 workflow semantics:

- manual PASS reaches REVIEWABLE with zero LLM calls (task 9.4);
- manual FAIL -> manual edit -> resubmit reaches REVIEWABLE;
- manual FAIL -> AI Fix -> resubmit reaches REVIEWABLE with exactly one
  semantic call, never auto-submitting/auto-approving (tasks 9.5/9.6);
- generated long-form: plan + K segment calls, all segment gates PASS,
  compile, full gate PASS -> REVIEWABLE (task 9.1/9.2);
- segment gate FAIL stops the product at N: no N+1..K-1 calls, no compile,
  no auto-repair (Decision 9);
- Full Script Gate FAIL -> GATE_FAILED with actionable violations mapped
  (task 9.3);
- human approval is the ONLY path to APPROVED (Decision 14).

The gate callables are fakes returning canned ``GateRunResult`` values; the
workflow itself never touches an LLM or the network.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import pytest

from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
)
from backend.application.script_authoring.models import (
    GateViolation,
    ScriptItem,
    ScriptSource,
    ScriptState,
)
from backend.application.script_authoring.state import IllegalTransitionError
from backend.application.script_authoring.workflow import (
    InvalidFixStateError,
    ProductGenerationWorkflow,
)

ITEM_ID = "script_item:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SET_ID = "script_set:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

EMPTY = ScriptState.EMPTY
DRAFT = ScriptState.DRAFT
GATE_FAILED = ScriptState.GATE_FAILED
GATE_RUNNING = ScriptState.GATE_RUNNING
GENERATING = ScriptState.GENERATING
PLANNING = ScriptState.PLANNING
REVIEWABLE = ScriptState.REVIEWABLE
APPROVED = ScriptState.APPROVED
FAILED = ScriptState.FAILED
AI_FIXING = ScriptState.AI_FIXING


# --- fakes ---------------------------------------------------------------------


def _violation(rule_id: str, *, segment_index: int | None = None) -> RuleViolation:
    return RuleViolation(
        rule_id=rule_id,
        severity=Severity.ERROR,
        message=f"{rule_id} violated",
        segment_index=segment_index,
    )


def _pass(_text: str | Sequence[str] = "", _target: float | None = None) -> GateRunResult:
    return GateRunResult(scope="segment", fingerprint=RuleSetFingerprint())


def _result(*violations: RuleViolation) -> GateRunResult:
    """A full-script gate RESULT carrying ``violations`` (not a callable)."""
    return GateRunResult(
        scope="full_script",
        violations=tuple(violations),
        fingerprint=RuleSetFingerprint(),
    )


def _fail(*violations: RuleViolation) -> Callable:
    """Build a full-gate callable that always fails with ``violations``."""

    def _check(_text: str | Sequence[str], _target: float | None = None) -> GateRunResult:
        return _result(*violations)

    return _check


@dataclass
class FakeContent:
    """Shape of an AI fix/generate result (matches SegmentGenerationResult)."""

    display_text: str
    spoken_text: str


@dataclass
class FakeWorkflow:
    """Boundary fakes + persisted state for one workflow under test."""

    workflow: ProductGenerationWorkflow
    persisted: list[ScriptItem] = field(default_factory=list)
    semantic_calls: int = 0

    def persist(self, item: ScriptItem) -> None:
        self.persisted.append(item.model_copy(deep=True))

    def generate(self) -> FakeContent:
        self.semantic_calls += 1
        return FakeContent(
            display_text="Bản sửa thủ công.",
            spoken_text="Bản sửa thủ công.",
        )


def _item(state: ScriptState = EMPTY) -> ScriptItem:
    return ScriptItem(
        id=ITEM_ID,
        script_set_id=SET_ID,
        product_id="P001",
        state=state,
    )


def _make(
    *,
    state: ScriptState = EMPTY,
    segment_gate: object = None,
    full_gate: object = None,
    with_generate: bool = False,
) -> FakeWorkflow:
    """Build a workflow with default passing gates (segment + full)."""
    sg = segment_gate if segment_gate is not None else _pass
    fg = full_gate if full_gate is not None else _pass
    fake = FakeWorkflow(workflow=None)  # type: ignore[arg-type]
    workflow = ProductGenerationWorkflow(
        item=_item(state),
        segment_gate=sg,  # type: ignore[arg-type]
        full_gate=fg,  # type: ignore[arg-type]
        persist=fake.persist,
        generate=fake.generate if with_generate else None,
    )
    fake.workflow = workflow
    return fake


def _drive_generated_long_form(fake: FakeWorkflow, k: int = 2) -> None:
    """Plan + generate K passing segments via the workflow surface."""
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=k)
    for index in range(k):
        wf.start_segment(index)
        wf.complete_segment(
            index,
            display_text=f"Đoạn {index}.",
            spoken_text=f"Đoạn {index}.",
        )


# --- 1. manual PASS zero-call (task 9.4) --------------------------------------


def test_manual_draft_pass_reaches_reviewable_with_zero_calls() -> None:
    fake = _make()
    wf = fake.workflow

    wf.create_manual_draft(display_text="Draft thủ công.", spoken_text="Draft thủ công.")
    assert wf.item.state is DRAFT
    assert wf.current_version is not None
    assert wf.current_version.source is ScriptSource.MANUAL

    result = wf.submit()
    assert result.passed
    assert wf.item.state is REVIEWABLE
    assert fake.semantic_calls == 0  # zero LLM calls
    assert fake.persisted  # every transition persisted


def test_manual_draft_pass_never_approves() -> None:
    """Gate PASS lands in REVIEWABLE — approval is a separate human step."""
    fake = _make()
    wf = fake.workflow
    wf.create_manual_draft(display_text="Draft.", spoken_text="Draft.")
    wf.submit()
    assert wf.item.state is REVIEWABLE
    assert wf.item.approved_version_id is None


# --- 2. manual FAIL -> manual edit -> resubmit --------------------------------


def test_manual_fail_then_manual_edit_then_resubmit() -> None:
    fake = _make(full_gate=_fail(_violation("FORMAT_PUNCTUATION")))
    wf = fake.workflow

    wf.create_manual_draft(display_text="Bad!!", spoken_text="Bad!!")
    result = wf.submit()
    assert not result.passed
    assert wf.item.state is GATE_FAILED
    assert wf.last_gate_run is not None
    assert wf.last_gate_run.passed is False
    assert wf.last_gate_run.violations[0].rule_id == "FORMAT_PUNCTUATION"

    # Manual edit from GATE_FAILED -> new DRAFT version.
    wf.create_manual_draft(display_text="Tốt rồi.", spoken_text="Tốt rồi.")
    assert wf.item.state is DRAFT
    assert len(wf.versions) == 2  # immutable history preserved

    # Resubmit with a passing gate.
    fake.workflow.full_gate = _pass  # type: ignore[attr-defined]
    result = wf.submit()
    assert result.passed
    assert wf.item.state is REVIEWABLE
    assert fake.semantic_calls == 0


# --- 3. manual FAIL -> AI Fix -> resubmit (tasks 9.5/9.6) ---------------------


def test_manual_fail_then_ai_fix_then_resubmit() -> None:
    fake = _make(
        full_gate=lambda texts: (
            _result(_violation("STYLE_EM_DASH")) if "—" in texts[0] else _pass()
        ),
        with_generate=True,
    )
    wf = fake.workflow

    wf.create_manual_draft(display_text="Xin chào—mọi người.", spoken_text="Xin chào—mọi người.")
    result = wf.submit()
    assert not result.passed
    assert wf.item.state is GATE_FAILED

    # Fix eligibility: only GATE_FAILED.
    assert wf.fix_eligible() is True
    wf.apply_ai_fix()
    assert fake.semantic_calls == 1
    assert wf.item.state is DRAFT  # never auto-approved, never auto-submitted
    assert wf.current_version is not None
    assert wf.current_version.source is ScriptSource.AI_FIX
    assert len(wf.versions) == 2  # new immutable version, history intact

    # Explicit submit again -> gate PASS -> REVIEWABLE (no auto-approve).
    result = wf.submit()
    assert result.passed
    assert wf.item.state is REVIEWABLE
    assert wf.item.approved_version_id is None


def test_ai_fix_requires_gate_failed_state() -> None:
    """fix_eligible raises InvalidFixStateError on any non-GATE_FAILED state."""
    for state in (EMPTY, DRAFT, GATE_RUNNING, REVIEWABLE, APPROVED, GENERATING):
        fake = _make(state=state, with_generate=True)
        with pytest.raises(InvalidFixStateError) as exc:
            fake.workflow.fix_eligible()
        assert exc.value.state is state


def test_ai_fix_uses_exactly_one_semantic_call() -> None:
    fake = _make(
        full_gate=lambda texts: _result(_violation("CLAIM_PRICE")),
        with_generate=True,
    )
    wf = fake.workflow
    wf.create_manual_draft(display_text="Giá 999k.", spoken_text="Giá 999k.")
    wf.submit()
    assert wf.item.state is GATE_FAILED
    wf.apply_ai_fix()
    assert fake.semantic_calls == 1


# --- 4. generated long-form PASS (tasks 9.1/9.2) ------------------------------


def test_generated_long_form_pass_plan_k_segments_compile_reviewable() -> None:
    fake = _make(with_generate=True)
    wf = fake.workflow

    wf.start_planning(target_duration_s=600)
    assert wf.item.state is PLANNING
    wf.complete_planning(segment_count=2)
    assert wf.item.state is GENERATING
    assert wf.plan_segment_count == 2

    wf.start_segment(0)
    wf.complete_segment(0, display_text="Đoạn 0.", spoken_text="Đoạn 0.")
    assert wf.item.state is GENERATING  # still generating between segments
    wf.start_segment(1)
    wf.complete_segment(1, display_text="Đoạn 1.", spoken_text="Đoạn 1.")

    # compile + full gate (all segments passed locally first)
    result = wf.compile_and_full_gate()
    assert result.passed
    assert wf.item.state is REVIEWABLE
    assert wf.last_compile is not None
    assert wf.last_compile.segment_texts == ("Đoạn 0.", "Đoạn 1.")
    assert wf.current_version is not None
    assert wf.current_version.segment_version_ids == [
        wf.segments[0].id,
        wf.segments[1].id,
    ]
    # planning + 2 segment calls, no extra summary call
    assert fake.semantic_calls == 0  # generate stub used only by fix path
    assert len(wf.versions) == 1


def test_compile_rejects_missing_segments() -> None:
    """Full gate runs only after ALL required segments pass (task 9.2)."""
    fake = _make(with_generate=True)
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=3)
    wf.start_segment(0)
    wf.complete_segment(0, display_text="Đoạn 0.", spoken_text="Đoạn 0.")
    with pytest.raises(ValueError, match="cannot compile"):
        wf.compile_and_full_gate()
    assert wf.item.state is GENERATING


# --- 5. generated segment FAIL (Decision 9) -----------------------------------


def test_segment_gate_fail_stops_product_at_n() -> None:
    segment_results = [_pass(), _result(_violation("PROFANITY_OFFENSIVE"))]

    def sg(text: str, _target: float | None = None) -> GateRunResult:
        return segment_results.pop(0)

    fake = _make(segment_gate=sg, with_generate=True)
    wf = fake.workflow

    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=3)

    wf.start_segment(0)
    wf.complete_segment(0, display_text="Đoạn 0.", spoken_text="Đoạn 0.")
    assert wf.item.state is GENERATING
    assert len(wf.segments) == 1

    wf.start_segment(1)
    result = wf.complete_segment(1, display_text="Xấu quá!", spoken_text="Xấu quá!")
    assert not result.passed
    assert wf.item.state is GATE_FAILED
    assert wf.segments[1].status is GATE_FAILED
    # No segment 2 ever runs: the gate failure stops scheduling N+1..K-1.
    assert len(wf.segments) == 2
    # No compile, no auto-repair, no auto-regenerate.
    with pytest.raises(IllegalTransitionError):
        wf.compile_and_full_gate()
    # Fix with AI IS legal from GATE_FAILED (task 9.5).
    assert wf.fix_eligible() is True

    # Manual edit from GATE_FAILED is the human resolution path.
    wf.create_manual_draft(display_text="Đoạn 1 tốt.", spoken_text="Đoạn 1 tốt.")
    assert wf.item.state is DRAFT


def test_segment_gate_receives_planned_per_segment_target() -> None:
    """The segment gate gets ``target_duration_s / K`` so its duration band can
    match the planned segment (15.4 finding: a fixed 10-180s default cannot
    hold a planned 200-240s segment). Manual drafts (no plan) pass None."""
    targets: list[float | None] = []

    def sg(text: str, target: float | None = None) -> GateRunResult:
        targets.append(target)
        return _pass(text, target)

    fake = _make(segment_gate=sg, with_generate=True)
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=3)
    for index in range(3):
        wf.start_segment(index)
        wf.complete_segment(index, display_text=f"Đoạn {index}.", spoken_text=f"Đoạn {index}.")
    assert targets == [200.0, 200.0, 200.0]


# --- 6. Full Gate FAIL (task 9.3) ---------------------------------------------


def test_full_gate_fail_maps_actionable_violations() -> None:
    fake = _make(
        full_gate=lambda texts: _result(
            _violation("REPETITION_CROSS", segment_index=1),
            _violation("CLAIM_CONTRADICTION"),
        ),
        with_generate=True,
    )
    wf = fake.workflow

    _drive_generated_long_form(fake, k=2)
    result = wf.compile_and_full_gate()
    assert not result.passed
    assert wf.item.state is GATE_FAILED
    assert wf.last_gate_run is not None
    assert wf.last_gate_run.passed is False

    # Violations mapped to persisted GateViolation rows (task 9.3).
    assert [v.rule_id for v in wf.last_gate_run.violations] == [
        "REPETITION_CROSS",
        "CLAIM_CONTRADICTION",
    ]
    assert wf.last_gate_run.violations[0].segment_index == 1
    assert wf.last_gate_run.violations[1].segment_index is None
    assert all(isinstance(v, GateViolation) for v in wf.last_gate_run.violations)


def test_full_gate_fail_does_not_auto_repair() -> None:
    calls = {"fix": 0}

    def bad_fix() -> FakeContent:
        calls["fix"] += 1
        return FakeContent(display_text="x", spoken_text="x")

    fake = _make(
        full_gate=lambda texts: _result(_violation("COVERAGE_REQUIRED")),
        with_generate=True,
    )
    fake.generate = bad_fix  # type: ignore[assignment]
    wf = fake.workflow

    _drive_generated_long_form(fake, k=2)
    wf.compile_and_full_gate()
    assert wf.item.state is GATE_FAILED
    assert calls["fix"] == 0  # no automatic semantic retry


def test_record_segment_attempt_selects_passed_candidate() -> None:
    # Reviewer R9.2/3.4: one candidate -> one gate eval -> one immutable
    # candidate + one GateRun bound to it; a passed candidate is SELECTED and
    # the workflow stays in GENERATING for the next index.
    fake = _make(with_generate=True)
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=2)
    wf.start_segment(0)
    candidate = wf.record_segment_attempt(
        0,
        display_text="Đoạn 0.",
        spoken_text="Đoạn 0.",
        attempt=1,
        gate_result=_pass("Đoạn 0.", 300.0),
    )
    assert candidate.status is DRAFT
    assert wf.segments[0].id == candidate.id
    assert wf.item.state is GENERATING
    assert wf.last_gate_run is not None
    assert wf.last_gate_run.passed is True
    assert wf.last_gate_run.full is False
    assert wf.last_gate_run.script_version_id == candidate.id


def test_record_segment_attempt_failed_candidate_is_evidence_not_selected() -> None:
    # Reviewer R9.2/3.4: a failed candidate is persisted as audit evidence but
    # NOT selected, so a bounded in-place retry of the SAME index stays legal
    # (no N+1 work while N is unresolved) and the item is not advanced.
    fake = _make(with_generate=True)
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=2)
    wf.start_segment(0)
    failed = _result(_violation("PROFANITY_OFFENSIVE"))
    candidate = wf.record_segment_attempt(
        0,
        display_text="Xấu quá!",
        spoken_text="Xấu quá!",
        attempt=1,
        gate_result=failed,
    )
    assert candidate.status is GATE_FAILED
    assert 0 not in wf.segments, "failed candidate is evidence, not the selected segment"
    assert wf.item.state is GENERATING
    assert wf.last_gate_run is not None
    assert wf.last_gate_run.passed is False
    assert [v.rule_id for v in wf.last_gate_run.violations] == ["PROFANITY_OFFENSIVE"]
    assert wf.last_gate_run.script_version_id == candidate.id


def test_fail_segment_gate_lands_gate_failed_after_budget_exhausted() -> None:
    # Reviewer R9.2: after the bounded segment auto-heal budget is exhausted,
    # the workflow lands truthful GATE_FAILED (no unbounded retry loop).
    fake = _make(with_generate=True)
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=2)
    wf.start_segment(0)
    wf.fail_segment_gate(0, message="segment 0 gate failed after 3 semantic attempts")
    assert wf.item.state is GATE_FAILED
    assert wf.last_error == "segment 0 gate failed after 3 semantic attempts"
    with pytest.raises(IllegalTransitionError):
        wf.complete_segment(1, display_text="x", spoken_text="x")


def test_full_gate_fail_persists_complete_failed_version() -> None:
    # Reviewer R9.2/3.7: when the Full Script Gate fails after all K segments
    # pass locally, a complete immutable compiled ScriptVersion (GATE_FAILED)
    # is persisted, bound to the exact full-gate violations, and becomes the
    # item's current version — so a later human Fix with AI operates on the
    # exact failed compiled script instead of an out-of-band reconstruction.
    fake = _make(
        full_gate=_fail(
            _violation("REPETITION_CROSS", segment_index=1),
            _violation("CLAIM_CONTRADICTION"),
        ),
        with_generate=True,
    )
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=2)
    wf.start_segment(0)
    wf.complete_segment(0, display_text="Đoạn 0.", spoken_text="Đoạn 0.")
    wf.start_segment(1)
    wf.complete_segment(1, display_text="Đoạn 1.", spoken_text="Đoạn 1.")
    result = wf.compile_and_full_gate()
    assert not result.passed
    assert wf.item.state is GATE_FAILED
    assert wf.current_version is not None
    assert wf.current_version.state is GATE_FAILED
    assert wf.current_version.segment_version_ids == [wf.segments[0].id, wf.segments[1].id]
    assert wf.current_version.gate_run_id == wf.last_gate_run.id
    assert wf.item.current_version_id == wf.current_version.id
    assert [v.rule_id for v in wf.last_gate_run.violations] == [
        "REPETITION_CROSS",
        "CLAIM_CONTRADICTION",
    ]
    # A failed compiled version is not approval and not REVIEWABLE.
    assert wf.item.approved_version_id is None
    assert wf.current_version.state is not REVIEWABLE
    # Human full-script Fix is legal from this exact failed version.
    assert wf.fix_eligible() is True


# --- 7. human approval (Decision 14) ------------------------------------------


def test_human_approval_is_only_path_to_approved() -> None:
    fake = _make(with_generate=True)
    wf = fake.workflow

    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=1)
    wf.start_segment(0)
    wf.complete_segment(0, display_text="Đoạn 0.", spoken_text="Đoạn 0.")
    wf.compile_and_full_gate()
    assert wf.item.state is REVIEWABLE

    wf.approve(actor="operator-1")
    assert wf.item.state is APPROVED
    assert wf.item.approved_version_id == wf.current_version.id


def test_approve_requires_reviewable() -> None:
    fake = _make(state=GATE_FAILED)
    with pytest.raises(IllegalTransitionError):
        fake.workflow.approve(actor="operator-1")


def test_gate_pass_cannot_auto_approve_even_with_warnings() -> None:
    """WARNING violations never fail the gate and never approve the script."""
    warning_run = GateRunResult(
        scope="full_script",
        violations=(
            RuleViolation(rule_id="TONE_CONSISTENCY", severity=Severity.WARNING, message="tone"),
        ),
        fingerprint=RuleSetFingerprint(),
    )
    fake = _make(full_gate=lambda texts: warning_run)
    wf = fake.workflow
    wf.create_manual_draft(display_text="Draft.", spoken_text="Draft.")
    result = wf.submit()
    assert result.passed  # warnings do not fail
    assert wf.item.state is REVIEWABLE
    assert wf.item.approved_version_id is None


# --- invalid transitions (task 9.1 typed errors) ------------------------------


@pytest.mark.parametrize(
    ("state", "method", "args"),
    [
        (DRAFT, "start_planning", {"target_duration_s": 600}),
        (GATE_FAILED, "start_planning", {"target_duration_s": 600}),
        (REVIEWABLE, "start_planning", {"target_duration_s": 600}),
        (APPROVED, "start_planning", {"target_duration_s": 600}),
        (DRAFT, "complete_planning", {"segment_count": 2}),
        (GENERATING, "complete_planning", {"segment_count": 2}),
        (EMPTY, "compile_and_full_gate", {}),
        (DRAFT, "compile_and_full_gate", {}),
        (EMPTY, "start_segment", {"index": 0}),
        (REVIEWABLE, "start_segment", {"index": 0}),
        (EMPTY, "complete_segment", {"index": 0, "display_text": "x", "spoken_text": "x"}),
        (EMPTY, "submit", {}),
        (GATE_RUNNING, "submit", {}),
        (APPROVED, "approve", {"actor": "operator-1"}),
        (GENERATING, "approve", {"actor": "operator-1"}),
    ],
)
def test_invalid_transitions_raise_typed_error(state, method, args) -> None:
    fake = _make(state=state, with_generate=True)
    wf = fake.workflow
    with pytest.raises(IllegalTransitionError):
        getattr(wf, method)(**args)


def test_segment_index_must_follow_fixed_bounds() -> None:
    fake = _make(with_generate=True)
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=2)
    wf.start_segment(0)
    wf.complete_segment(0, display_text="Đoạn 0.", spoken_text="Đoạn 0.")
    # Out-of-bounds K is rejected (index beyond K-1).
    with pytest.raises(ValueError, match="out of fixed bounds"):
        wf.start_segment(9)
    # Segment indices are strictly sequential: a valid-but-wrong index is
    # rejected before any semantic call.
    with pytest.raises(ValueError, match="sequential"):
        wf.start_segment(0)  # 0 already generated, next must be 1
    # The actual next index is legal.
    wf.start_segment(1)


def test_generation_failure_is_terminal() -> None:
    fake = _make(with_generate=True)
    wf = fake.workflow
    wf.start_planning(target_duration_s=600)
    wf.complete_planning(segment_count=2)
    wf.segment_failed(0, message="provider timeout")
    assert wf.item.state is FAILED
    assert wf.last_error == "provider timeout"
    # Terminal: no further transitions are legal.
    with pytest.raises(IllegalTransitionError):
        wf.complete_segment(1, display_text="x", spoken_text="x")


def test_every_transition_persisted() -> None:
    """Each state change is reflected in the persisted item snapshots."""
    fake = _make(with_generate=True)
    wf = fake.workflow
    wf.create_manual_draft(display_text="Draft.", spoken_text="Draft.")
    wf.submit()

    observed = [snapshot.state for snapshot in fake.persisted]
    assert observed == [DRAFT, GATE_RUNNING, REVIEWABLE]
