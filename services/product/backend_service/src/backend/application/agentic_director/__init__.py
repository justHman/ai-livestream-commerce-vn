"""Bounded agentic director: typed plan/result contracts and evidence ops.

Public surface:
- ``contracts``: plans (``FactualFastPlan``, ``ComplexPlan``), discriminated
  results (``PlanResult`` / ``AnswerText`` / ``UnavailableAnswer`` /
  ``BudgetExceeded``) and ``VerbalizationRequest``.
- ``evidence_ops``: allowlisted evidence operations, validation and dispatch.
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

__all__ = [
    "AnswerText",
    "BudgetExceeded",
    "ComplexPlan",
    "EvidenceRequest",
    "EvidenceExecutor",
    "EvidenceOperation",
    "EvidenceOperationRejected",
    "FactualFastPlan",
    "PlanKind",
    "PlanResult",
    "UnavailableAnswer",
    "VerbalizationRequest",
    "ALLOWED_EVIDENCE_OPS",
    "execute_evidence_operation",
    "validate_evidence_operation",
]
