"""Offline tests for the bounded complex path (tasks 12.5, 12.6).

The planner, evidence executor and final generator are the system boundaries;
all three are faked here because their live implementations arrive in later
clusters. Asserted behavior only: budget counts, typed terminal results, and
never executing work beyond the budget.
"""

from __future__ import annotations

from typing import Callable

from backend.application.agentic_director.complex_path import (
    AgentBudgets,
    ComplexPathExecutor,
    PlanningOutput,
)
from backend.application.agentic_director.contracts import (
    ComplexPlan,
    EvidenceRequest,
    PlanKind,
)


class FakePlanner:
    """Returns one canned planning output and records every call."""

    def __init__(self, output: PlanningOutput) -> None:
        self.output = output
        self.calls = 0

    def plan(self, request: ComplexPlan) -> PlanningOutput:
        self.calls += 1
        return self.output


class FakeEvidenceExecutor:
    """Serves one configured result batch per round and records every call."""

    def __init__(self, round_results: list[list[dict]]) -> None:
        self.round_results = list(round_results)
        self.calls: list[tuple] = []

    def search_entities(
        self, queries: tuple[str, ...], entity_type: str | None = None
    ) -> list[dict]:
        self.calls.append(("search_entities", queries, entity_type))
        return self._next()

    def get_entities(
        self, entity_ids: tuple[str, ...], selectors: tuple[str, ...] | None = None
    ) -> list[dict]:
        self.calls.append(("get_entities", entity_ids, selectors))
        return self._next()

    def get_evidence(self, requests: tuple) -> list[dict]:
        self.calls.append(("get_evidence", requests))
        return self._next()

    def _next(self) -> list[dict]:
        return self.round_results.pop(0) if self.round_results else []


class FakeFinalGenerator:
    """Returns a canned grounded answer and records every call."""

    def __init__(self, text: str = "Grounded answer") -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def generate(self, evidence_summary: str, question_context: str) -> str:
        self.calls.append((evidence_summary, question_context))
        return self.text


def _record_sink() -> tuple[dict[str, int | float], Callable[[str, int | float], None]]:
    values: dict[str, int | float] = {}

    def emit(name: str, value: int | float) -> None:
        values[name] = value

    return values, emit


def _plan(*requests: EvidenceRequest) -> ComplexPlan:
    return ComplexPlan(
        intent="so sánh giá",
        entities=("sp-1", "sp-2"),
        evidence_requests=requests,
    )


def _execute(
    *,
    planner: FakePlanner,
    executor: FakeEvidenceExecutor,
    final: FakeFinalGenerator,
    budgets: AgentBudgets | None = None,
    metric_sink: Callable[[str, int | float], None] | None = None,
):
    return ComplexPathExecutor().run_plan(
        _plan(),
        envelope=object(),
        planner=planner,
        evidence_executor=executor,
        final_generator=final,
        budgets=budgets,
        metric_sink=metric_sink,
    )


def test_normal_comparison_uses_exactly_one_planning_round_and_final_generation():
    planner = FakePlanner(
        PlanningOutput(
            plan=_plan(
                EvidenceRequest(selector="price", entity_id="sp-1"),
                EvidenceRequest(selector="price", entity_id="sp-2"),
            ),
            raw_text="compare prices",
        )
    )
    executor = FakeEvidenceExecutor(
        [
            [
                {"entity_id": "sp-1", "selector": "price", "value": "29.000"},
                {"entity_id": "sp-2", "selector": "price", "value": "35.000"},
            ]
        ]
    )
    final = FakeFinalGenerator("Áo thun A giá 29.000, áo thun B giá 35.000.")
    sink, emit = _record_sink()

    result = _execute(planner=planner, executor=executor, final=final, metric_sink=emit)

    assert result.kind == PlanKind.ANSWER
    assert result.answer.text == "Áo thun A giá 29.000, áo thun B giá 35.000."
    assert planner.calls == 1
    assert len(executor.calls) == 1
    assert len(final.calls) == 1
    assert final.calls[0][1] == "so sánh giá"
    assert "29.000" in final.calls[0][0]
    assert sink["planning_generations"] == 1
    assert sink["evidence_rounds"] == 1
    assert sink["final_generations"] == 1
    assert sink["llm_calls"] == 2
    assert sink["evidence_ops"] == 1


def test_planner_output_with_disallowed_evidence_op_is_unavailable():
    planner = FakePlanner(
        PlanningOutput(
            plan=ComplexPlan(
                intent="compare",
                entities=("sp-1",),
                evidence_requests=({"op": "read_file", "path": "/etc/passwd"},),
            ),
            raw_text="read a file",
        )
    )
    executor = FakeEvidenceExecutor([])
    final = FakeFinalGenerator()

    result = _execute(planner=planner, executor=executor, final=final)

    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable.reason == "plan_invalid"
    assert executor.calls == []
    assert final.calls == []


def test_planner_output_with_missing_evidence_fields_is_unavailable():
    planner = FakePlanner(
        PlanningOutput(
            plan=ComplexPlan(
                intent="compare",
                entities=("sp-1",),
                evidence_requests=({"op": "get_entities"},),
            ),
            raw_text="get entities",
        )
    )
    executor = FakeEvidenceExecutor([])
    final = FakeFinalGenerator()

    result = _execute(planner=planner, executor=executor, final=final)

    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable.reason == "plan_invalid"
    assert executor.calls == []


def test_planner_output_without_a_plan_is_unavailable():
    planner = FakePlanner(PlanningOutput(plan=None, raw_text="cannot decide"))
    executor = FakeEvidenceExecutor([])
    final = FakeFinalGenerator()

    result = _execute(planner=planner, executor=executor, final=final)

    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable.reason == "plan_invalid"
    assert executor.calls == []
    assert final.calls == []


def test_second_round_attempt_without_exceptional_budget_is_budget_exceeded():
    planner = FakePlanner(
        PlanningOutput(
            plan=_plan(
                EvidenceRequest(selector="price", entity_id="sp-1"),
                EvidenceRequest(selector="stock", entity_id="sp-2"),
            ),
            raw_text="needs stock too",
        )
    )
    # Round 1 resolves only one request, so a second round is required — but
    # the normal budget forbids it.
    executor = FakeEvidenceExecutor(
        [[{"entity_id": "sp-1", "selector": "price", "value": "29.000"}]]
    )
    final = FakeFinalGenerator()

    result = _execute(planner=planner, executor=executor, final=final)

    assert result.kind == PlanKind.BUDGET_EXCEEDED
    assert result.budget.op == "evidence_rounds"
    assert result.budget.limit == 1
    assert result.budget.used == 2
    assert len(executor.calls) == 1
    assert final.calls == []


def test_exceptional_second_round_runs_once_when_required():
    budgets = AgentBudgets(allow_exceptional_round=True)
    planner = FakePlanner(
        PlanningOutput(
            plan=_plan(
                EvidenceRequest(selector="price", entity_id="sp-1"),
                EvidenceRequest(selector="stock", entity_id="sp-2"),
            ),
            raw_text="needs stock too",
        )
    )
    executor = FakeEvidenceExecutor(
        [
            [{"entity_id": "sp-1", "selector": "price", "value": "29.000"}],
            [{"entity_id": "sp-2", "selector": "stock", "value": "còn hàng"}],
        ]
    )
    final = FakeFinalGenerator("Cả hai còn hàng.")
    sink, emit = _record_sink()

    result = _execute(
        planner=planner, executor=executor, final=final, budgets=budgets, metric_sink=emit
    )

    assert result.kind == PlanKind.ANSWER
    assert len(executor.calls) == 2
    # Round 2 requested ONLY the still-missing request.
    assert executor.calls[1] == ("get_evidence", ({"selector": "stock", "entity_id": "sp-2"},))
    assert sink["evidence_rounds"] == 2
    assert sink["evidence_ops"] == 2
    assert sink["llm_calls"] == 2


def test_third_round_attempt_exceeds_exceptional_ceiling():
    budgets = AgentBudgets(allow_exceptional_round=True)
    planner = FakePlanner(
        PlanningOutput(
            plan=_plan(
                EvidenceRequest(selector="price", entity_id="sp-1"),
                EvidenceRequest(selector="stock", entity_id="sp-2"),
                EvidenceRequest(selector="warranty", entity_id="sp-3"),
            ),
            raw_text="needs three facts",
        )
    )
    # Both allowed rounds resolve one request each; the third is still needed.
    executor = FakeEvidenceExecutor(
        [
            [{"entity_id": "sp-1", "selector": "price", "value": "29.000"}],
            [{"entity_id": "sp-2", "selector": "stock", "value": "còn hàng"}],
        ]
    )
    final = FakeFinalGenerator()

    result = _execute(planner=planner, executor=executor, final=final, budgets=budgets)

    assert result.kind == PlanKind.BUDGET_EXCEEDED
    assert result.budget.op == "evidence_rounds"
    assert result.budget.limit == 2
    assert result.budget.used == 3
    assert len(executor.calls) == 2
    assert final.calls == []


def test_evidence_round_without_any_value_is_unavailable():
    planner = FakePlanner(
        PlanningOutput(
            plan=_plan(EvidenceRequest(selector="price", entity_id="sp-1")),
            raw_text="price",
        )
    )
    executor = FakeEvidenceExecutor([[]])
    final = FakeFinalGenerator()

    result = _execute(planner=planner, executor=executor, final=final)

    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []


def test_null_evidence_values_never_reach_the_answer():
    planner = FakePlanner(
        PlanningOutput(
            plan=_plan(EvidenceRequest(selector="price", entity_id="sp-1")),
            raw_text="price",
        )
    )
    executor = FakeEvidenceExecutor([[{"entity_id": "sp-1", "selector": "price", "value": None}]])
    final = FakeFinalGenerator()

    result = _execute(planner=planner, executor=executor, final=final)

    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []


def test_planning_budget_of_zero_blocks_the_planning_generation():
    result = _execute(
        planner=FakePlanner(PlanningOutput(plan=None, raw_text="")),
        executor=FakeEvidenceExecutor([]),
        final=FakeFinalGenerator(),
        budgets=AgentBudgets(max_planning_generations=0),
    )

    assert result.kind == PlanKind.BUDGET_EXCEEDED
    assert result.budget.op == "planning"
    assert result.budget.limit == 0
    assert result.budget.used == 1


def test_final_generation_budget_of_zero_never_calls_the_generator():
    planner = FakePlanner(
        PlanningOutput(
            plan=_plan(EvidenceRequest(selector="price", entity_id="sp-1")),
            raw_text="price",
        )
    )
    executor = FakeEvidenceExecutor(
        [[{"entity_id": "sp-1", "selector": "price", "value": "29.000"}]]
    )
    final = FakeFinalGenerator()

    result = _execute(
        planner=planner,
        executor=executor,
        final=final,
        budgets=AgentBudgets(max_final_generations=0),
    )

    assert result.kind == PlanKind.BUDGET_EXCEEDED
    assert result.budget.op == "final_generation"
    assert result.budget.limit == 0
    assert result.budget.used == 1
    assert final.calls == []
