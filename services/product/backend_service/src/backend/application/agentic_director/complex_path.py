"""Bounded complex-agent path: generation/evidence budgets, tasks 12.5, 12.6.

Design Decision 14: the complex path (comparison, ambiguity, referential
follow-ups, synthesis) runs under code-owned budgets — at most one planning
generation, normally one batch evidence round, and one final answer
generation. A second evidence round is possible ONLY when explicitly
configured (``allow_exceptional_round``) up to the exceptional ceiling. The
executor never lets an autonomous loop continue: any attempt beyond a budget
yields a typed ``BUDGET_EXCEEDED`` result with the op that crossed it.

Trust boundary (Decision 13): the planner output is UNTRUSTED. Its typed plan
carries evidence requests through a runtime-unguarded tuple, so the executor
re-validates every item at the allowlist boundary — ``EvidenceRequest`` items
are batched into one ``get_evidence`` op, raw dict items (e.g. a model
proposing ``read_file``) are validated as standalone operation-shaped dicts.
Any rejection -> ``plan_invalid`` with zero executor calls.

Rounds: one batch round executes every validated op once. A second round is
demanded deterministically only when evidence results resolve SOME but not
ALL typed requests (missing selectors). Results with no resolvable values ->
``evidence_unavailable``; the runtime never invents values. The ``envelope``
is reserved for later-cluster cluster/telemetry attribution — this layer
grounds on the typed plan plus evidence only.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable

from backend.application.agentic_director.contracts import (
    AnswerText,
    BudgetExceeded,
    ComplexPlan,
    EvidenceRequest,
    PlanKind,
    PlanResult,
    UnavailableAnswer,
)
from backend.application.agentic_director.evidence_ops import (
    EvidenceExecutor,
    EvidenceOperation,
    EvidenceOperationRejected,
    execute_evidence_operation,
    validate_evidence_operation,
)


@dataclass(frozen=True, slots=True)
class AgentBudgets:
    """Code-owned generation/evidence budgets for the complex path."""

    max_planning_generations: int = 1
    max_evidence_rounds: int = 1
    max_evidence_rounds_exceptional: int = 2
    max_final_generations: int = 1
    allow_exceptional_round: bool = False


@dataclass(frozen=True, slots=True)
class PlanningOutput:
    """One planning generation: the model-produced plan (or None) + raw text."""

    plan: ComplexPlan | None
    raw_text: str


@runtime_checkable
class PlanPlanner(Protocol):
    """One planning generation (implemented by a later cluster)."""

    def plan(self, request: ComplexPlan) -> PlanningOutput: ...


@runtime_checkable
class FinalGenerator(Protocol):
    """One final answer generation from grounded evidence only."""

    def generate(self, evidence_summary: str, question_context: str) -> str: ...


def _unavailable_result(reason: str) -> PlanResult:
    return PlanResult(kind=PlanKind.UNAVAILABLE, unavailable=UnavailableAnswer(reason=reason))


def _budget_result(op: str, used: int, limit: int) -> PlanResult:
    return PlanResult(
        kind=PlanKind.BUDGET_EXCEEDED,
        budget=BudgetExceeded(limit=limit, used=used, op=op),
    )


def _build_evidence_ops(plan: ComplexPlan) -> list[EvidenceOperation]:
    """Validate the model-produced plan into allowlisted evidence operations."""
    ops: list[EvidenceOperation] = []
    typed: list[dict] = []
    for item in plan.evidence_requests:
        if isinstance(item, EvidenceRequest):
            typed.append({"selector": item.selector, "entity_id": item.entity_id})
        else:
            # Untrusted raw model dict (e.g. "read_file"): allowlist-revalidate
            # it as its own operation-shaped dict; anything else also raises.
            ops.append(validate_evidence_operation(item))
    if typed:
        ops.append(validate_evidence_operation({"op": "get_evidence", "requests": tuple(typed)}))
    return ops


def _request_resolved(request: EvidenceRequest, evidence: list[dict]) -> bool:
    """True when some evidence result carries a value for this request."""
    for result in evidence:
        value = result.get("value")
        if value is None or value == "":
            continue
        if "selector" in result and result["selector"] != request.selector:
            continue
        if (
            request.entity_id
            and result.get("entity_id")
            and result["entity_id"] != request.entity_id
        ):
            continue
        return True
    return False


def _pending_requests(
    requests: tuple[EvidenceRequest, ...], evidence: list[dict]
) -> list[EvidenceRequest]:
    """Requests still missing a value in the collected evidence."""
    return [r for r in requests if not _request_resolved(r, evidence)]


def _any_resolved(evidence: list[dict]) -> bool:
    return any(r.get("value") not in (None, "") for r in evidence)


def _build_evidence_summary(evidence: list[dict]) -> str:
    """Grounded-evidence-only summary for the final generator."""
    parts = []
    for result in evidence:
        subject = result.get("selector") or result.get("entity_id") or "?"
        parts.append(f"{subject}={result.get('value')}")
    return "; ".join(parts)


class ComplexPathExecutor:
    """Executes a ``ComplexPlan`` under code-owned generation/evidence budgets."""

    def run_plan(
        self,
        plan: ComplexPlan,
        envelope: object,
        planner: PlanPlanner,
        evidence_executor: EvidenceExecutor,
        final_generator: FinalGenerator,
        budgets: AgentBudgets | None = None,
        metric_sink: Callable[[str, int | float], None] | None = None,
    ) -> PlanResult:
        started = time.monotonic()
        budgets = budgets or AgentBudgets()

        def emit(name: str, value: int | float) -> None:
            if metric_sink is not None:
                metric_sink(name, value)

        def finish(
            result: PlanResult,
            *,
            rounds: int,
            finals: int,
            ops: int,
            planning: int = 1,
        ) -> PlanResult:
            emit("planning_generations", planning)
            emit("evidence_rounds", rounds)
            emit("final_generations", finals)
            emit("llm_calls", planning + finals)
            emit("evidence_ops", ops)
            emit("latency_ms", int((time.monotonic() - started) * 1000))
            return result

        # One planning generation; the model never gets a second one.
        used_planning = 1
        if used_planning > budgets.max_planning_generations:
            return finish(
                _budget_result("planning", used_planning, budgets.max_planning_generations),
                rounds=0,
                finals=0,
                ops=0,
            )
        output = planner.plan(plan)
        if output.plan is None:
            return finish(_unavailable_result("plan_invalid"), rounds=0, finals=0, ops=0)

        # Re-validate the model-produced plan at the allowlist boundary.
        try:
            ops = _build_evidence_ops(output.plan)
        except EvidenceOperationRejected:
            return finish(_unavailable_result("plan_invalid"), rounds=0, finals=0, ops=0)
        if not ops:
            return finish(_unavailable_result("evidence_unavailable"), rounds=0, finals=0, ops=0)

        ceiling = (
            budgets.max_evidence_rounds_exceptional
            if budgets.allow_exceptional_round
            else budgets.max_evidence_rounds
        )
        requests = output.plan.evidence_requests
        evidence: list[dict] = []
        used_rounds = 0
        ops_executed = 0
        pending = _pending_requests(requests, evidence)
        while pending or used_rounds == 0:
            used_rounds += 1
            if used_rounds > ceiling:
                # The attempt is counted but never executed.
                return finish(
                    _budget_result("evidence_rounds", used_rounds, ceiling),
                    rounds=used_rounds - 1,
                    finals=0,
                    ops=ops_executed,
                )
            if used_rounds == 1:
                round_ops = ops
            else:
                round_ops = [
                    validate_evidence_operation(
                        {
                            "op": "get_evidence",
                            "requests": tuple(
                                {"selector": r.selector, "entity_id": r.entity_id} for r in pending
                            ),
                        }
                    )
                ]
            for op in round_ops:
                evidence.extend(execute_evidence_operation(evidence_executor, op))
                ops_executed += 1
            pending = _pending_requests(requests, evidence)
            if pending and not _any_resolved(evidence):
                return finish(
                    _unavailable_result("evidence_unavailable"),
                    rounds=used_rounds,
                    finals=0,
                    ops=ops_executed,
                )
        if not _any_resolved(evidence):
            return finish(
                _unavailable_result("evidence_unavailable"),
                rounds=used_rounds,
                finals=0,
                ops=ops_executed,
            )

        # One final answer generation from grounded evidence only.
        used_final = 1
        if used_final > budgets.max_final_generations:
            return finish(
                _budget_result("final_generation", used_final, budgets.max_final_generations),
                rounds=used_rounds,
                finals=0,
                ops=ops_executed,
            )
        text = final_generator.generate(_build_evidence_summary(evidence), plan.intent)
        first = evidence[0]
        return finish(
            PlanResult(
                kind=PlanKind.ANSWER,
                answer=AnswerText(
                    text=text,
                    source_entity_id=str(first.get("entity_id", "")),
                    source_selector=str(first.get("selector", "")),
                ),
            ),
            rounds=used_rounds,
            finals=1,
            ops=ops_executed,
        )
