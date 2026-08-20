"""WorkflowDriver (Change B, B3): step / is_terminal / snapshot / restore.

RED before ``generation/driver.py`` exists: import fails. GREEN once the
adapter wraps the FSM and drives the batch protocol correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field


from backend.application.script_authoring.generation.continuity import ContinuityState
from backend.application.script_authoring.generation.driver import WorkflowDriver
from backend.application.script_authoring.generation.segment_generator import (
    SegmentGenerationResult,
    SegmentStepOutcome,
)
from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
)
from backend.application.script_authoring.models import (
    GateRun,
    ScriptItem,
    ScriptSegment,
    ScriptState,
    ScriptVersion,
    new_id,
)
from backend.application.script_authoring.workflow import ProductGenerationWorkflow


def _gate_pass(_text, _target: float | None = None) -> GateRunResult:
    return GateRunResult(scope="segment", fingerprint=RuleSetFingerprint())


def _gate_fail(_text, _target: float | None = None) -> GateRunResult:
    return GateRunResult(
        scope="segment",
        fingerprint=RuleSetFingerprint(),
        violations=(
            RuleViolation(
                rule_id="format.vn",
                severity=Severity.ERROR,
                message="bad",
            ),
        ),
    )


def _item(state: ScriptState = ScriptState.EMPTY) -> ScriptItem:
    return ScriptItem(
        id=new_id("script_item"), script_set_id=new_id("script_set"), product_id="p1", state=state
    )


@dataclass
class _Fake:
    workflow: ProductGenerationWorkflow | None = None
    persisted: list[object] = field(default_factory=list)

    def persist(self, obj: object) -> None:
        self.persisted.append(obj)

    def make_workflow(self, *, item: ScriptItem, segment_gate, full_gate, generate=None):
        self.workflow = ProductGenerationWorkflow(
            item=item,
            segment_gate=segment_gate,
            full_gate=full_gate,
            persist=self.persist,
            generate=generate,
        )
        return self.workflow


def _segment_outcome(index: int, text: str) -> SegmentStepOutcome:
    return SegmentStepOutcome(
        index=index,
        state=ContinuityState(),
        result=SegmentGenerationResult(
            segment_index=index,
            display_text=f"display {index}",
            spoken_text=text,
        ),
    )


def _plan_generate(segment_count: int):
    return lambda: (
        segment_count,
        [
            {"title": f"s{i}", "intent": "section", "target_duration_s": 300}
            for i in range(segment_count)
        ],
    )


def _segment_generate(spoken: list[str]):
    calls: list[int] = []

    def _gen(
        index: int, state: ContinuityState, _target: float | None = None
    ) -> SegmentStepOutcome:
        calls.append(index)
        return _segment_outcome(index, spoken[index])

    _gen.calls = calls  # type: ignore[attr-defined]
    return _gen


def _segment_generate_retry(spoken_by_index: dict[int, list[str]]):
    """Segment generator where index ``i`` yields its texts in call order."""
    calls: list[int] = []
    counters: dict[int, int] = {}

    def _gen(
        index: int, state: ContinuityState, _target: float | None = None
    ) -> SegmentStepOutcome:
        calls.append(index)
        texts = spoken_by_index[index]
        n = counters.get(index, 0)
        counters[index] = n + 1
        return _segment_outcome(index, texts[min(n, len(texts) - 1)])

    _gen.calls = calls  # type: ignore[attr-defined]
    return _gen


def _gate_pass_after_bad(text: str, _target: float | None = None) -> GateRunResult:
    """Pass unless the segment text ends with ``-bad`` (drives retry tests)."""
    if text.endswith("-bad"):
        return _gate_fail(text, _target)
    return _gate_pass(text, _target)


def _build_driver(
    fake: _Fake,
    *,
    segment_gate,
    full_gate,
    plan_generate,
    segment_generate,
    loaders: dict | None = None,
    max_segment_attempts: int = 3,
) -> WorkflowDriver:
    wf = fake.make_workflow(
        item=_item(),
        segment_gate=segment_gate,
        full_gate=full_gate,
        generate=lambda *a, **k: object(),
    )
    wf.target_duration_s = 600
    loaders = loaders or {}

    def _load_item(item_id: str) -> ScriptItem:
        return _item()

    def _load_segment(segment_id: str) -> ScriptSegment:
        for seg in fake.persisted:
            if isinstance(seg, ScriptSegment) and seg.id == segment_id:
                return seg
        raise KeyError(segment_id)

    def _load_version(version_id: str) -> ScriptVersion:
        for version in fake.persisted:
            if isinstance(version, ScriptVersion) and version.id == version_id:
                return version
        raise KeyError(version_id)

    return WorkflowDriver(
        product_id="p1",
        workflow=wf,
        plan_generate=plan_generate,
        segment_generate=segment_generate,
        persist=fake.persist,
        load_item=loaders.get("item", _load_item),
        load_segment=loaders.get("segment", _load_segment),
        load_version=loaders.get("version", _load_version),
        max_segment_attempts=max_segment_attempts,
    )


def test_driver_steps_empty_to_reviewable() -> None:
    fake = _Fake()
    gen = _segment_generate(["seg-a", "seg-b"])
    driver = _build_driver(
        fake,
        segment_gate=_gate_pass,
        full_gate=_gate_pass,
        plan_generate=_plan_generate(2),
        segment_generate=gen,
    )
    steps = 0
    while driver.step():
        steps += 1
        assert steps <= 10, "driver must terminate within a finite phase count"
    assert driver.workflow.item.state is ScriptState.REVIEWABLE
    assert gen.calls == [0, 1]
    # Persisted: item(s) via FSM + plan + 2 segments + 1 version + gate runs.
    assert sum(isinstance(o, ScriptSegment) for o in fake.persisted) == 2
    assert sum(isinstance(o, ScriptVersion) for o in fake.persisted) == 1
    # One GateRun per semantic candidate (2 segment) + 1 full-gate run (R9.2:
    # each candidate is gated exactly once and its run is persisted).
    assert sum(isinstance(o, GateRun) for o in fake.persisted) == 3
    assert driver.workflow.semantic_calls == 0  # driver counts separately, not via FSM


def test_driver_segment_gate_fail_stops_before_later_segments() -> None:
    fake = _Fake()
    gen = _segment_generate(["seg-a", "seg-b"])
    driver = _build_driver(
        fake,
        segment_gate=_gate_fail,  # fails at segment 0 every attempt
        full_gate=_gate_pass,
        plan_generate=_plan_generate(2),
        segment_generate=gen,
        max_segment_attempts=1,  # no in-place retry -> fail immediately
    )
    while driver.step():
        pass
    assert driver.workflow.item.state is ScriptState.GATE_FAILED
    assert gen.calls == [0], "no semantic calls for N+1..K-1 after a segment gate failure"
    # The single failed candidate is persisted as audit evidence (R9.2).
    seg0 = [o for o in fake.persisted if isinstance(o, ScriptSegment) and o.segment_index == 0]
    assert len(seg0) == 1 and seg0[0].status is ScriptState.GATE_FAILED
    assert driver.is_terminal()


def test_driver_segment_inplace_retry_passes_then_reviewable() -> None:
    """A segment that fails the gate is regenerated in place, not the whole script.

    15.4 money optimization: a real LLM fails a segment gate 1-2 words at a
    time; regenerating the WHOLE script discards the passing segments and
    costs K+1 calls per retry. In-place retry regenerates only the failed
    segment (keeping continuity), so a later attempt can still reach
    REVIEWABLE without re-generating earlier segments. Every semantic attempt
    is persisted once (failed candidates stay auditable, reviewer R9.2).
    """
    fake = _Fake()
    # Segment 0 produces a gate-failing text on its first attempt, a passing
    # one on its second; segment 1 passes first try.
    gen = _segment_generate_retry({0: ["seg-a-bad", "seg-a-good"], 1: ["seg-b"]})
    driver = _build_driver(
        fake,
        segment_gate=_gate_pass_after_bad,
        full_gate=_gate_pass,
        plan_generate=_plan_generate(2),
        segment_generate=gen,
        max_segment_attempts=3,
    )
    while driver.step():
        pass
    assert driver.workflow.item.state is ScriptState.REVIEWABLE
    # Segment 0 was regenerated once (bad -> good); segment 1 was never re-run.
    assert gen.calls == [0, 0, 1], "failed segment regenerated in place, earlier segment kept"
    # Persisted: seg0 attempt1 (GATE_FAILED evidence) + seg0 attempt2 (selected
    # DRAFT) + seg1 (DRAFT) = 3 immutable candidate rows.
    segments = [o for o in fake.persisted if isinstance(o, ScriptSegment)]
    assert len(segments) == 3
    seg0_rows = [s for s in segments if s.segment_index == 0]
    assert [s.status for s in seg0_rows] == [ScriptState.GATE_FAILED, ScriptState.DRAFT]
    assert [s.version for s in seg0_rows] == [1, 2]
    # The selected segment for index 0 is the passing candidate.
    assert driver.workflow.segments[0].spoken_text == "seg-a-good"
    # One GateRun per semantic candidate (3 segment) + 1 full-gate run.
    assert sum(isinstance(o, GateRun) for o in fake.persisted) == 4


def test_driver_segment_inplace_retry_exhausted_gate_failed() -> None:
    """After max attempts the segment lands GATE_FAILED (human fix path)."""
    fake = _Fake()
    gen = _segment_generate_retry({0: ["seg-a-bad", "seg-a-bad", "seg-a-bad", "seg-b"]})
    driver = _build_driver(
        fake,
        segment_gate=_gate_pass_after_bad,
        full_gate=_gate_pass,
        plan_generate=_plan_generate(2),
        segment_generate=gen,
        max_segment_attempts=3,
    )
    while driver.step():
        pass
    assert driver.workflow.item.state is ScriptState.GATE_FAILED
    assert gen.calls == [0, 0, 0], "segment 0 retried exactly max_segment_attempts times"
    # All 3 failed candidates persisted as auditable attempt evidence (R9.2).
    seg0 = [o for o in fake.persisted if isinstance(o, ScriptSegment) and o.segment_index == 0]
    assert len(seg0) == 3
    assert all(s.status is ScriptState.GATE_FAILED for s in seg0)
    assert [s.version for s in seg0] == [1, 2, 3]
    assert driver.is_terminal()


def test_driver_snapshot_restore_roundtrip_without_rerun() -> None:
    fake = _Fake()
    gen = _segment_generate(["seg-a", "seg-b"])
    driver = _build_driver(
        fake,
        segment_gate=_gate_pass,
        full_gate=_gate_pass,
        plan_generate=_plan_generate(2),
        segment_generate=gen,
    )
    # Advance through planning + segment 0 only.
    driver.step()  # EMPTY -> PLANNING
    driver.step()  # PLANNING -> GENERATING (plan persisted)
    driver.step()  # segment 0
    assert driver.workflow.item.state is ScriptState.GENERATING
    assert len(driver.workflow.segments) == 1

    snapshot = driver.snapshot()

    # A fresh workflow + driver restores from the snapshot.
    fake2 = _Fake()
    gen2 = _segment_generate(["seg-a", "seg-b"])
    restored = _build_driver(
        fake2,
        segment_gate=_gate_pass,
        full_gate=_gate_pass,
        plan_generate=_plan_generate(2),
        segment_generate=gen2,
        loaders={"segment": _segment_loader(fake)},
    )
    restored.restore(snapshot)
    assert restored.workflow.item.state is ScriptState.GENERATING
    assert restored.workflow.plan_segment_count == 2
    assert len(restored.workflow.segments) == 1

    # Resume: the next step must generate segment 1, NOT re-run segment 0.
    restored.step()
    assert gen2.calls == [1], "restore must resume at the next unresolved segment"


def _segment_loader(fake: _Fake):
    def _load(segment_id: str) -> ScriptSegment:
        for seg in fake.persisted:
            if isinstance(seg, ScriptSegment) and seg.id == segment_id:
                return seg
        raise KeyError(segment_id)

    return _load


def test_driver_is_terminal_before_complete_is_false() -> None:
    fake = _Fake()
    driver = _build_driver(
        fake,
        segment_gate=_gate_pass,
        full_gate=_gate_pass,
        plan_generate=_plan_generate(1),
        segment_generate=_segment_generate(["only"]),
    )
    assert driver.is_terminal() is False
