"""Legal state transitions for script items (task 2.2).

Encodes the Decision 2 diagram exactly and deterministically:

    EMPTY
      ├─ manual edit ───────────────► DRAFT
      └─ Generate Script ─► PLANNING/GENERATING ─► DRAFT
    DRAFT
      └─ Submit ─► GATE_RUNNING
                      ├─ PASS ─► REVIEWABLE
                      └─ FAIL ─► GATE_FAILED
                                    ├─ manual edit ─► DRAFT
                                    └─ Fix with AI ─► AI_FIXING ─► DRAFT
    REVIEWABLE
      └─ Human Approve ─► APPROVED
    Any content/dependency-invalidating edit:
    REVIEWABLE/APPROVED ─► new DRAFT or STALE

Gate PASS never means approved (state stays REVIEWABLE until an explicit
human approval command). AI generation/repair never means approved (they
produce a new DRAFT). ``state.py`` is pure: repository and service layers
persist the outcome of ``transition``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from backend.application.script_authoring.models import ScriptState

__all__ = ["IllegalTransitionError", "transition", "legal_targets"]

#: User-level transition verbs understood by the API/workflow layer.
TransitionVerb = Literal[
    "manual_edit",
    "generate",
    "plan_ready",
    "generation_complete",
    "generation_failed",
    "cancel",
    "submit",
    "gate_pass",
    "gate_fail",
    "ai_fix",
    "ai_fix_complete",
    "approve",
    "invalidate",
]

_TRANSITIONS: dict[ScriptState, dict[TransitionVerb, ScriptState]] = {
    ScriptState.EMPTY: {
        "manual_edit": ScriptState.DRAFT,
        "generate": ScriptState.PLANNING,
        "cancel": ScriptState.CANCELLED,
    },
    ScriptState.PLANNING: {
        "plan_ready": ScriptState.GENERATING,
        "generation_failed": ScriptState.FAILED,
        "cancel": ScriptState.CANCELLED,
    },
    ScriptState.GENERATING: {
        "generation_complete": ScriptState.DRAFT,
        "generation_failed": ScriptState.FAILED,
        "cancel": ScriptState.CANCELLED,
    },
    ScriptState.DRAFT: {
        "manual_edit": ScriptState.DRAFT,
        "submit": ScriptState.GATE_RUNNING,
    },
    ScriptState.GATE_RUNNING: {
        "gate_pass": ScriptState.REVIEWABLE,
        "gate_fail": ScriptState.GATE_FAILED,
    },
    ScriptState.GATE_FAILED: {
        "manual_edit": ScriptState.DRAFT,
        "ai_fix": ScriptState.AI_FIXING,
    },
    ScriptState.AI_FIXING: {
        "ai_fix_complete": ScriptState.DRAFT,
        "generation_failed": ScriptState.FAILED,
        "cancel": ScriptState.CANCELLED,
    },
    ScriptState.REVIEWABLE: {
        "approve": ScriptState.APPROVED,
        # Any content/dependency-invalidating edit invalidates approval.
        "manual_edit": ScriptState.DRAFT,
        "invalidate": ScriptState.STALE,
    },
    ScriptState.APPROVED: {
        "manual_edit": ScriptState.DRAFT,
        "invalidate": ScriptState.STALE,
    },
    ScriptState.STALE: {
        "manual_edit": ScriptState.DRAFT,
        "submit": ScriptState.GATE_RUNNING,
    },
    # Terminal states: no outgoing transitions (a new draft starts fresh).
    ScriptState.CANCELLED: {},
    ScriptState.FAILED: {},
}


class IllegalTransitionError(ValueError):
    """Raised when a transition verb is not legal from the current state.

    Carries stable machine-readable fields so the API layer can map the
    error to HTTP 409 with a domain error code.
    """

    def __init__(self, state: ScriptState, verb: TransitionVerb) -> None:
        self.state = state
        self.verb = verb
        super().__init__(f"illegal transition {verb!r} from state {state.value!r}")


def legal_targets(state: ScriptState) -> frozenset[ScriptState]:
    """Return the deterministic set of states reachable from ``state``."""
    return frozenset(_TRANSITIONS[state].values())


def transition(state: ScriptState, verb: TransitionVerb) -> ScriptState:
    """Return the target state for ``verb`` from ``state``.

    Deterministic: identical (state, verb) input always yields the same
    result or the same ``IllegalTransitionError``. Never mutates input.
    """
    try:
        targets = _TRANSITIONS[state]
    except KeyError as exc:  # pragma: no cover - enum is exhaustive
        raise IllegalTransitionError(state, verb) from exc
    if verb not in targets:
        raise IllegalTransitionError(state, verb)
    return targets[verb]
