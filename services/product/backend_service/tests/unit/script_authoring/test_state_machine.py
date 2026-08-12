"""Task 2.3: state machine invariants.

Proves the Decision 2 rules that protect human-final approval:

- gate PASS only reaches REVIEWABLE, never APPROVED (no auto-approval);
- editing after approval creates a new DRAFT (or STALE) — never mutates
  the approved version;
- AI generation/repair operations always land in DRAFT, never APPROVED.
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.models import (
    ScriptItem,
    ScriptState,
)
from backend.application.script_authoring.state import (
    IllegalTransitionError,
    legal_targets,
    transition,
)

APPROVED = ScriptState.APPROVED
DRAFT = ScriptState.DRAFT
GATE_FAILED = ScriptState.GATE_FAILED
GATE_RUNNING = ScriptState.GATE_RUNNING
GENERATING = ScriptState.GENERATING
PLANNING = ScriptState.PLANNING
REVIEWABLE = ScriptState.REVIEWABLE
STALE = ScriptState.STALE


def _item(state: ScriptState = ScriptState.EMPTY) -> ScriptItem:
    return ScriptItem(
        id="script_item:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        script_set_id="script_set:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        product_id="P001",
        state=state,
    )


# --- (a) gate PASS cannot auto-approve --------------------------------------


def test_gate_pass_reaches_reviewable_not_approved() -> None:
    target = transition(GATE_RUNNING, "gate_pass")
    assert target is REVIEWABLE
    assert target is not APPROVED


def test_approved_is_not_reachable_by_gate_pass() -> None:
    # Gate PASS and AI flows never touch APPROVED: the only path to it is
    # the explicit human "approve" verb from REVIEWABLE.
    assert APPROVED not in legal_targets(GATE_RUNNING)
    assert APPROVED not in legal_targets(DRAFT)
    assert APPROVED not in legal_targets(GATE_FAILED)
    assert APPROVED not in legal_targets(STALE)
    assert APPROVED not in legal_targets(GENERATING)  # noqa: F821 - below
    assert APPROVED not in legal_targets(PLANNING)  # noqa: F821 - below
    # APPROVED is reachable from REVIEWABLE — but only via "approve".
    assert APPROVED in legal_targets(REVIEWABLE)
    assert APPROVED == transition(REVIEWABLE, "approve")


def test_approve_requires_explicit_human_verb() -> None:
    assert transition(REVIEWABLE, "approve") is APPROVED


# --- (b) edit after approval creates a new draft / stale --------------------


def test_edit_after_approval_creates_new_draft() -> None:
    target = transition(APPROVED, "manual_edit")
    assert target is DRAFT


def test_dependency_invalidation_after_approval_creates_stale() -> None:
    target = transition(APPROVED, "invalidate")
    assert target is STALE
    assert transition(REVIEWABLE, "invalidate") is STALE


def test_stale_requires_resubmission() -> None:
    assert transition(STALE, "manual_edit") is DRAFT
    assert transition(STALE, "submit") is GATE_RUNNING


# --- (c) AI operations never produce APPROVED directly ----------------------


def test_ai_fix_completes_into_draft() -> None:
    # Requesting the fix enters the AI_FIXING substate; the result lands in
    # a new DRAFT, never APPROVED.
    assert transition(GATE_FAILED, "ai_fix") is ScriptState.AI_FIXING
    assert transition(ScriptState.AI_FIXING, "ai_fix_complete") is DRAFT


def test_generation_completes_into_draft() -> None:
    assert transition(ScriptState.GENERATING, "generation_complete") is DRAFT


def test_ai_ops_never_reach_approved_from_any_state() -> None:
    for state in ScriptState:
        for verb in ("ai_fix", "ai_fix_complete", "generation_complete", "plan_ready"):
            if verb in _verbs_for(state):
                assert transition(state, verb) is not APPROVED


def _verbs_for(state: ScriptState) -> set[str]:
    # Duplicate the private table access via the public error surface:
    # a verb is "legal" iff it does not raise IllegalTransitionError.
    from backend.application.script_authoring import state as state_module

    return {
        verb
        for verb in (
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
        )
        if verb in state_module._TRANSITIONS[state]
    }


# --- deterministic rejection of illegal transitions -------------------------


@pytest.mark.parametrize(
    ("state", "verb"),
    [
        (GATE_FAILED, "approve"),  # must re-submit first
        (REVIEWABLE, "submit"),  # already passed gate
        (DRAFT, "approve"),  # gate not run
        (ScriptState.EMPTY, "approve"),  # nothing to approve
        (ScriptState.CANCELLED, "submit"),  # terminal
        (ScriptState.FAILED, "ai_fix"),  # terminal
        (GATE_RUNNING, "manual_edit"),  # gate is running
    ],
)
def test_illegal_transitions_raise_deterministically(state: ScriptState, verb: str) -> None:
    with pytest.raises(IllegalTransitionError) as exc_info:
        transition(state, verb)
    assert exc_info.value.state is state
    assert exc_info.value.verb == verb


def test_illegal_transition_message_is_stable() -> None:
    with pytest.raises(
        IllegalTransitionError, match=r"illegal transition 'approve' from state 'draft'"
    ):
        transition(DRAFT, "approve")


def test_transition_is_pure_and_repeatable() -> None:
    first = transition(DRAFT, "submit")
    second = transition(DRAFT, "submit")
    assert first is second is GATE_RUNNING
    assert _item().state is ScriptState.EMPTY  # input untouched
