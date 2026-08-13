"""Allowlisted evidence operations for the bounded agentic director (C12).

Trust boundary (design Decision 13): the model may request evidence only
through these typed operations. The executor surface below is the ONLY thing
exposed to plan execution — this module deliberately does NOT provide any
general tool (no shell, filesystem, HTTP, or job-management operation). Any
other op name is rejected at validation AND at dispatch.

The live ``EvidenceExecutor`` implementation is provided by a later cluster;
here it is a duck-typed Protocol so this module stays independently testable
with a minimal fake.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Exactly the evidence ops from design Decision 13.
ALLOWED_EVIDENCE_OPS: frozenset[str] = frozenset(
    {"search_entities", "get_entities", "get_evidence"}
)

# Per-op required args and their expected types.
_OP_ARGS: dict[str, dict[str, type]] = {
    "search_entities": {"queries": tuple},
    "get_entities": {"entity_ids": tuple},
    "get_evidence": {"requests": tuple},
}

# Optional typed args per op (present -> must be the expected type).
_OP_OPTIONAL: dict[str, dict[str, type]] = {
    "get_entities": {"selectors": tuple},
}


class EvidenceOperationRejected(Exception):
    """A model-requested evidence operation failed allowlist validation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True, slots=True)
class EvidenceOperation:
    """A validated evidence operation: allowlisted op name + typed args."""

    op: str
    queries: tuple[str, ...] = ()
    entity_ids: tuple[str, ...] = ()
    selectors: tuple[str, ...] = ()
    requests: tuple = ()


@runtime_checkable
class EvidenceExecutor(Protocol):
    """Duck-typed evidence executor (implemented by a later cluster)."""

    def search_entities(
        self, queries: tuple[str, ...], entity_type: str | None = None
    ) -> list[dict]: ...

    def get_entities(
        self, entity_ids: tuple[str, ...], selectors: tuple[str, ...] | None = None
    ) -> list[dict]: ...

    def get_evidence(self, requests: tuple) -> list[dict]: ...


def validate_evidence_operation(raw: dict) -> EvidenceOperation:
    """Validate a model-requested evidence operation against the allowlist.

    Strict on purpose — this is the trust boundary for model output:
    unknown op names, missing/wrong-typed args, and unknown extra fields are
    all rejected with a typed ``EvidenceOperationRejected`` error.
    """
    if not isinstance(raw, dict):
        raise EvidenceOperationRejected(
            "ev_op_rejected", f"evidence op must be a mapping, got {type(raw).__name__}"
        )
    op = raw.get("op")
    if op not in ALLOWED_EVIDENCE_OPS:
        raise EvidenceOperationRejected("ev_op_rejected", f"evidence op {op!r} is not allowlisted")
    args = _OP_ARGS[op]
    allowed = set(args) | set(_OP_OPTIONAL.get(op, {}))
    unknown = set(raw) - {"op"} - allowed
    if unknown:
        raise EvidenceOperationRejected(
            "ev_op_rejected",
            f"evidence op {op!r} has unknown fields: {sorted(unknown)}",
        )
    for name, expected in args.items():
        if name not in raw:
            raise EvidenceOperationRejected(
                "ev_op_rejected", f"evidence op {op!r} is missing required field {name!r}"
            )
        if not isinstance(raw[name], expected):
            raise EvidenceOperationRejected(
                "ev_op_rejected",
                f"evidence op {op!r} field {name!r} must be a {expected.__name__}",
            )
    for name, expected in _OP_OPTIONAL.get(op, {}).items():
        if name in raw and raw[name] is not None and not isinstance(raw[name], expected):
            raise EvidenceOperationRejected(
                "ev_op_rejected",
                f"evidence op {op!r} field {name!r} must be a {expected.__name__}",
            )
    return EvidenceOperation(
        op=op,
        queries=tuple(raw.get("queries", ())),
        entity_ids=tuple(raw.get("entity_ids", ())),
        selectors=tuple(raw.get("selectors", ())),
        requests=tuple(raw.get("requests", ())),
    )


def execute_evidence_operation(
    executor: EvidenceExecutor, operation: EvidenceOperation
) -> list[dict]:
    """Dispatch a validated operation to the executor.

    The op is re-checked against the allowlist here (belt and braces) so a
    hand-built ``EvidenceOperation`` can never reach the executor surface.
    """
    if operation.op not in ALLOWED_EVIDENCE_OPS:
        raise EvidenceOperationRejected(
            "ev_op_rejected", f"evidence op {operation.op!r} is not allowlisted"
        )
    if operation.op == "search_entities":
        return executor.search_entities(operation.queries)
    if operation.op == "get_entities":
        return executor.get_entities(operation.entity_ids, operation.selectors)
    return executor.get_evidence(operation.requests)
