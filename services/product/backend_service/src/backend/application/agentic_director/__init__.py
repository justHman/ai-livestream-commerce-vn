"""Bounded agentic director: typed plan/result contracts, evidence ops, fast path.

Public surface:
- ``contracts``: plans (``FactualFastPlan``, ``ComplexPlan``), discriminated
  results (``PlanResult`` / ``AnswerText`` / ``UnavailableAnswer`` /
  ``BudgetExceeded``) and ``VerbalizationRequest``.
- ``evidence_ops``: allowlisted evidence operations, validation and dispatch.
- ``fast_path``: deterministic factual fast path — eligibility evaluation,
  exact templated answers from authoritative evidence, and one bounded
  verbalization generation.
- ``telemetry``: content-safe execution telemetry (path, evidence cache,
  rounds, LLM calls, tokens, latency, terminal state).
- ``runtime_authority``: backend-owned runtime operations and the gate that
  rejects model-requested scheduling/retry/cursor/job instructions.
"""

from backend.application.agentic_director.contracts import (
    AnswerText,
    BudgetExceeded,
    ComplexPlan,
    EvidenceRequest,
    FactualFastPlan,
    PlanKind,
    PlanResult,
    UnavailableAnswer,
    VerbalizationRequest,
)
from backend.application.agentic_director.evidence_ops import (
    ALLOWED_EVIDENCE_OPS,
    EvidenceExecutor,
    EvidenceOperation,
    EvidenceOperationRejected,
    execute_evidence_operation,
    validate_evidence_operation,
)
from backend.application.agentic_director.runtime_authority import (
    FORBIDDEN_MODEL_INSTRUCTIONS,
    RuntimeAuthority,
    RuntimeInstructionRejected,
    assert_no_model_runtime_authority,
)
from backend.application.agentic_director.telemetry import (
    ExecutionTelemetry,
    InMemoryMetricSink,
    MetricSink,
    build_execution_telemetry,
)
from backend.application.agentic_director.fast_path import (
    ClusterEnvelope,
    FactProvider,
    FactValue,
    FastPathConfig,
    FastPathEligibility,
    FastPathExecutor,
    UntemplatedSelectorError,
    Verbalizer,
    build_templated_answer,
    is_fast_path_eligible,
    select_fact_selector,
)

__all__ = [
    "AnswerText",
    "BudgetExceeded",
    "ClusterEnvelope",
    "ComplexPlan",
    "EvidenceRequest",
    "EvidenceExecutor",
    "EvidenceOperation",
    "EvidenceOperationRejected",
    "ExecutionTelemetry",
    "FactProvider",
    "FactValue",
    "FactualFastPlan",
    "FastPathConfig",
    "FastPathEligibility",
    "FastPathExecutor",
    "FORBIDDEN_MODEL_INSTRUCTIONS",
    "InMemoryMetricSink",
    "MetricSink",
    "PlanKind",
    "PlanResult",
    "RuntimeAuthority",
    "RuntimeInstructionRejected",
    "UnavailableAnswer",
    "UntemplatedSelectorError",
    "VerbalizationRequest",
    "Verbalizer",
    "ALLOWED_EVIDENCE_OPS",
    "assert_no_model_runtime_authority",
    "build_execution_telemetry",
    "build_templated_answer",
    "execute_evidence_operation",
    "is_fast_path_eligible",
    "select_fact_selector",
    "validate_evidence_operation",
]
