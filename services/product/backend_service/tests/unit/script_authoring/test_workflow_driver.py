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
    ScriptItem,
    ScriptSegment,
    ScriptState,
    ScriptVersion,
    new_id,
)
from backend.application.script_authoring.workflow import ProductGenerationWorkflow


def _gate_pass(_text) -> GateRunResult:
    return GateRunResult(scope="segment", fingerprint=RuleSetFingerprint())


def _gate_fail(_text) -> GateRunResult:
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

    def _gen(index: int, state: ContinuityState) -> SegmentStepOutcome:
        calls.append(index)
        return _segment_outcome(index, spoken[index])

    _gen.calls = calls  # type: ignore[attr-defined]
    return _gen


def _build_driver(
    fake: _Fake,
    *,
    segment_gate,
    full_gate,
    plan_generate,
    segment_generate,
    loaders: dict | None = None,
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
    assert driver.workflow.semantic_calls == 0  # driver counts separately, not via FSM


def test_driver_segment_gate_fail_stops_before_later_segments() -> None:
    fake = _Fake()
    gen = _segment_generate(["seg-a", "seg-b"])
    driver = _build_driver(
        fake,
        segment_gate=_gate_fail,  # fails at segment 0
        full_gate=_gate_pass,
        plan_generate=_plan_generate(2),
        segment_generate=gen,
    )
    while driver.step():
        pass
    assert driver.workflow.item.state is ScriptState.GATE_FAILED
    assert gen.calls == [0], "no semantic calls for N+1..K-1 after a segment gate failure"
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
