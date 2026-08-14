"""Backend-owned runtime authority boundary for the bounded agentic director.

Requirement "Backend owns orchestration authority": the model SHALL NOT own
retries, candidate selection, pivot policy, script cursor mutation, or job
creation. Model output is untrusted content — it may name evidence selectors
and nothing else about execution — so those operations are owned by backend
code and surfaced through this module as a typed boundary.

The ``RuntimeAuthority`` Protocol lists the backend-owned operations the
runtime exposes (``record_metric``, ``revalidate_volatile_evidence``,
``is_within_budget``). They exist to make the ownership boundary testable;
they are NOT model-callable tools. Any module that tries to route a
model-requested scheduling/retry/cursor/job instruction through the authority
must raise ``RuntimeInstructionRejected`` — the deterministic gate below
proves model output cannot reach scheduling authority.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

__all__ = [
    "RuntimeAuthority",
    "RuntimeInstructionRejected",
    "FORBIDDEN_MODEL_INSTRUCTIONS",
    "assert_no_model_runtime_authority",
]

# Instruction classes the model must never be able to express. A payload key
# matching any of these (or containing such a key path) is a rejected
# scheduling/retry/cursor/job instruction, not evidence.
FORBIDDEN_MODEL_INSTRUCTIONS: frozenset[str] = frozenset(
    {"retry", "pivot", "script_cursor", "job", "interrupt", "schedule"}
)


class RuntimeInstructionRejected(Exception):
    """A model-requested runtime instruction was refused by the authority."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@runtime_checkable
class RuntimeAuthority(Protocol):
    """Backend-owned operations the runtime exposes to plan execution.

    Deliberately NOT model-callable: these are the operations the model
    cannot request through any payload. The protocol exists so the boundary
    is testable with a minimal fake before the live runtime lands.
    """

    def record_metric(self, name: str, value: int | float) -> None: ...

    def revalidate_volatile_evidence(self, entity_id: str) -> bool: ...

    def is_within_budget(self, op: str) -> bool: ...


def assert_no_model_runtime_authority(payload: Mapping) -> None:
    """Reject any payload that expresses a forbidden runtime instruction.

    The deterministic gate proving model output cannot reach scheduling
    authority: any key that is in ``FORBIDDEN_MODEL_INSTRUCTIONS``, or whose
    path contains such a key, raises ``RuntimeInstructionRejected`` (code
    "rt_instr_rejected"). Evidence payloads pass untouched.
    """
    for key, value in payload.items():
        key_text = key if isinstance(key, str) else str(key)
        if key_text in FORBIDDEN_MODEL_INSTRUCTIONS:
            raise RuntimeInstructionRejected(
                "rt_instr_rejected",
                f"model payload key {key!r} names a forbidden runtime instruction",
            )
        if isinstance(value, Mapping):
            assert_no_model_runtime_authority(value)
        elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            for item in value:
                if isinstance(item, (Mapping, Sequence)) and not isinstance(item, (str, bytes)):
                    assert_no_model_runtime_authority(item)  # type: ignore[arg-type]
