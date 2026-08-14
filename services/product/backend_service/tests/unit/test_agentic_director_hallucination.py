"""Hallucination regression: unavailable evidence is never replaced (task 12.12).

The viewer asks the price of a product whose authoritative price evidence is
missing or stale. Across BOTH paths the runtime MUST produce a typed
unavailable/clarifying result and MUST NOT invent an exact price. This is the
regression guard for the spec requirement "Authoritative evidence wins over
model claims" — the answer text never echoes a number the evidence did not
provide, including numbers the model itself claimed in its raw plan output.
Deterministic: no randomness, no network, no time-of-day dependence.
"""

from __future__ import annotations

import re

import pytest

from backend.application.agentic_director.complex_path import (
    AgentBudgets,
    ComplexPathExecutor,
    PlanningOutput,
)
from backend.application.agentic_director.contracts import (
    ComplexPlan,
    EvidenceRequest,
    FactualFastPlan,
    PlanKind,
)
from backend.application.agentic_director.evidence_ops import (
    EvidenceOperationRejected,
    validate_evidence_operation,
)
from backend.application.agentic_director.fast_path import (
    FastPathConfig,
    FastPathExecutor,
    FactValue,
)

PRICE_PATTERN = re.compile(r"\d[\d.,]*\s*(?:k|đ|₫|nghìn|triệu|đồng)")


class FakeFactProvider:
    """Dict-backed fact provider recording every get_fact call."""

    def __init__(self, facts: dict[tuple[str, str], FactValue | None]) -> None:
        self.facts = facts
        self.calls: list[tuple[str, str]] = []

    def get_fact(self, entity_id: str, selector: str) -> FactValue | None:
        self.calls.append((entity_id, selector))
        return self.facts.get((entity_id, selector))


class FakeEvidenceExecutor:
    """Records every call and serves canned result batches per round."""

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


class FakePlanner:
    """Returns one fixed planning output and records every call."""

    def __init__(self, output: PlanningOutput) -> None:
        self.output = output
        self.calls: list[ComplexPlan] = []

    def plan(self, request: ComplexPlan) -> PlanningOutput:
        self.calls.append(request)
        return self.output


class FakeFinalGenerator:
    """Returns canned text and records every call."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[tuple[str, str]] = []

    def generate(self, evidence_summary: str, question_context: str) -> str:
        self.calls.append((evidence_summary, question_context))
        return self.text


# --- 12.12a fast path: missing fact -----------------------------------------


def test_fast_path_missing_fact_returns_unavailable_without_price_pattern():
    result = FastPathExecutor().run_plan(
        FactualFastPlan("P099", "commerce.price.current"),
        envelope=object(),
        evidence_provider=FakeFactProvider({}),
        verbalizer=None,
        config=FastPathConfig(),
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert PRICE_PATTERN.search(result.unavailable.reason) is None


# --- 12.12b fast path: stale volatile fact ----------------------------------


def test_fast_path_stale_price_fact_returns_unavailable_without_price_pattern():
    result = FastPathExecutor().run_plan(
        FactualFastPlan("P099", "commerce.price.current"),
        envelope=object(),
        evidence_provider=FakeFactProvider(
            {("P099", "commerce.price.current"): FactValue("299.000đ", fresh=False)}
        ),
        verbalizer=None,
        config=FastPathConfig(),
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert PRICE_PATTERN.search(result.unavailable.reason) is None


# --- 12.12c complex path: evidence comes back empty -------------------------


def test_complex_path_empty_evidence_returns_unavailable_without_price_pattern():
    plan = ComplexPlan(
        intent="giá",
        entities=("P099",),
        evidence_requests=(EvidenceRequest(selector="commerce.price.current", entity_id="P099"),),
    )
    planner = FakePlanner(PlanningOutput(plan=plan, raw_text="price"))
    evidence = FakeEvidenceExecutor([[]])
    final = FakeFinalGenerator("never called")
    result = ComplexPathExecutor().run_plan(
        plan,
        envelope=object(),
        planner=planner,
        evidence_executor=evidence,
        final_generator=final,
        budgets=AgentBudgets(),
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []
    assert PRICE_PATTERN.search(result.unavailable.reason) is None


def test_complex_path_null_value_evidence_returns_unavailable():
    plan = ComplexPlan(
        intent="giá",
        entities=("P099",),
        evidence_requests=(EvidenceRequest(selector="commerce.price.current", entity_id="P099"),),
    )
    planner = FakePlanner(PlanningOutput(plan=plan, raw_text="price"))
    evidence = FakeEvidenceExecutor(
        [[{"entity_id": "P099", "selector": "commerce.price.current", "value": None}]]
    )
    final = FakeFinalGenerator("never called")
    result = ComplexPathExecutor().run_plan(
        plan,
        envelope=object(),
        planner=planner,
        evidence_executor=evidence,
        final_generator=final,
        budgets=AgentBudgets(),
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []


# --- 12.12d complex path: model claims a price but requests no evidence -----


def test_complex_path_model_price_claim_without_evidence_is_never_echoed():
    plan = ComplexPlan(
        intent="giá",
        entities=("P099",),
        evidence_requests=(),
        reasoning_hint="the price is 299.000đ",
    )
    planner = FakePlanner(
        PlanningOutput(
            plan=plan,
            raw_text="The price of P099 is 299.000đ, I am sure of it.",
        )
    )
    evidence = FakeEvidenceExecutor([])
    final = FakeFinalGenerator("never called")
    result = ComplexPathExecutor().run_plan(
        plan,
        envelope=object(),
        planner=planner,
        evidence_executor=evidence,
        final_generator=final,
        budgets=AgentBudgets(),
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []
    assert PRICE_PATTERN.search(result.unavailable.reason) is None
    assert "299.000đ" not in result.unavailable.reason


def test_complex_path_model_claim_skips_evidence_but_evidence_resolves_nothing():
    plan = ComplexPlan(
        intent="giá",
        entities=("P099",),
        evidence_requests=(EvidenceRequest(selector="commerce.price.original", entity_id="P099"),),
        reasoning_hint="the price is 299.000đ",
    )
    planner = FakePlanner(
        PlanningOutput(
            plan=plan,
            raw_text="The price is 299.000đ. Also check the original price.",
        )
    )
    evidence = FakeEvidenceExecutor([[]])
    final = FakeFinalGenerator("never called")
    result = ComplexPathExecutor().run_plan(
        plan,
        envelope=object(),
        planner=planner,
        evidence_executor=evidence,
        final_generator=final,
        budgets=AgentBudgets(),
    )
    assert result.kind == PlanKind.UNAVAILABLE
    assert result.unavailable is not None
    assert result.unavailable.reason == "evidence_unavailable"
    assert final.calls == []
    assert "299.000đ" not in result.unavailable.reason


# --- 12.7/12.8 cross-check: allowlist rejects general tools -----------------


def test_validate_evidence_operation_rejects_read_file():
    with pytest.raises(EvidenceOperationRejected) as excinfo:
        validate_evidence_operation({"op": "read_file", "path": "/etc/passwd"})
    assert excinfo.value.code == "ev_op_rejected"
    assert "read_file" in excinfo.value.message


def test_validate_evidence_operation_rejects_http_get():
    with pytest.raises(EvidenceOperationRejected) as excinfo:
        validate_evidence_operation({"op": "http_get", "url": "https://example.com"})
    assert excinfo.value.code == "ev_op_rejected"
    assert "http_get" in excinfo.value.message
