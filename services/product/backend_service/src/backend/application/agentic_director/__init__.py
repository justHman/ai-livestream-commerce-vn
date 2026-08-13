"""Bounded agentic director: typed plan/result contracts, evidence ops, fast path.

Public surface:
- ``contracts``: plans (``FactualFastPlan``, ``ComplexPlan``), discriminated
  results (``PlanResult`` / ``AnswerText`` / ``UnavailableAnswer`` /
  ``BudgetExceeded``) and ``VerbalizationRequest``.
- ``evidence_ops``: allowlisted evidence operations, validation and dispatch.
- ``fast_path``: deterministic factual fast path — eligibility evaluation,
  exact templated answers from authoritative evidence, and one bounded
  verbalization generation.
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
    "FactProvider",
    "FactValue",
    "FactualFastPlan",
    "FastPathConfig",
    "FastPathEligibility",
    "FastPathExecutor",
    "PlanKind",
    "PlanResult",
    "UnavailableAnswer",
    "UntemplatedSelectorError",
    "VerbalizationRequest",
    "Verbalizer",
    "ALLOWED_EVIDENCE_OPS",
    "build_templated_answer",
    "execute_evidence_operation",
    "is_fast_path_eligible",
    "select_fact_selector",
    "validate_evidence_operation",
]
