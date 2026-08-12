"""Human-only, dependency-bound approval service (tasks 4.6/4.7).

Decision 14: approval requires (a) the exact current compiled ScriptVersion,
(b) a successful latest Full Script Gate run for THAT exact version, (c) no
stale fact/promotion/persona/rule dependencies, and (d) an authenticated
human action. Gate PASS never means approved; only this service creates an
immutable approval record.

The gate-check seam is a ``GateChecker`` callable protocol — Cluster 3's
engine plugs in at integration. ``is_approval_fresh`` is the staleness
predicate used by session binding (task 4.7/12.x): an approval that no
longer matches the current dependency versions cannot bind to runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from backend.application.script_authoring.fingerprints import (
    ApprovalDependencies,
    approval_dependency_hash,
)
from backend.application.script_authoring.gate.results import GateRunResult

__all__ = [
    "ApprovalError",
    "GateChecker",
    "ApprovalRequest",
    "ApprovalRecord",
    "approve_script",
    "is_approval_fresh",
]


class ApprovalError(Exception):
    """Raised when an approval precondition fails (Decision 14)."""


class GateChecker(Protocol):
    """Latest Full Script Gate check for an exact compiled version.

    Cluster 3's ``ScriptGate`` engine implements this at integration: given
    the exact compiled spoken text and segment versions, it returns the
    latest full-script ``GateRunResult`` for that exact version. The
    protocol keeps this module decoupled from the engine so the approval
    service stays a pure policy seam.
    """

    def check_full_script(self, compiled_spoken_text: str) -> GateRunResult: ...


@dataclass(frozen=True)
class ApprovalRequest:
    """What a human approval command binds to (Decision 14)."""

    script_item_id: str
    script_version_id: str
    compiled_spoken_text: str
    segment_version_ids: tuple[str, ...]
    plan_version: int
    rule_set_key: str
    product_facts_version: str
    promotion_version: str
    persona_brief_version: str
    actor_id: str
    is_human: bool
    # Authorized to approve (existing admin/operator capability, task 14.4).
    authorized: bool = False


@dataclass(frozen=True)
class ApprovalRecord:
    """Immutable approval result created by ``approve_script``."""

    id: str
    script_item_id: str
    script_version_id: str
    actor_id: str
    approval_hash: str
    gate_run_id: str
    dependencies: ApprovalDependencies


def approve_script(
    request: ApprovalRequest,
    *,
    gate_checker: GateChecker,
    new_approval_id: str,
) -> ApprovalRecord:
    """Approve the exact version when every Decision-14 precondition holds.

    Refuses (raises ``ApprovalError``) when the actor is not an authorized
    human, when the latest Full Script Gate for the exact compiled version
    did not pass, or when the gate run scope is not a full-script run.

    The returned record binds the immutable approval hash over the exact
    dependency versions; any later dependency change makes
    ``is_approval_fresh`` false (task 4.5/4.7).
    """
    if not request.is_human:
        raise ApprovalError("approval requires an authenticated human actor")
    if not request.authorized:
        raise ApprovalError("actor is not authorized to approve scripts")

    gate_result = gate_checker.check_full_script(request.compiled_spoken_text)
    if gate_result.scope != "full_script":
        raise ApprovalError(f"expected a full_script gate run, got scope {gate_result.scope!r}")
    if not gate_result.passed:
        raise ApprovalError(
            "latest Full Script Gate for this exact version did not pass; approval refused"
        )

    dependencies = ApprovalDependencies(
        spoken_text=request.compiled_spoken_text,
        segment_hashes=tuple(request.segment_version_ids),
        plan_version=request.plan_version,
        rule_set=request.rule_set_key,
        product_facts_version=request.product_facts_version,
        promotion_version=request.promotion_version,
        persona_brief_version=request.persona_brief_version,
    )
    approval_hash = approval_dependency_hash(dependencies)

    return ApprovalRecord(
        id=new_approval_id,
        script_item_id=request.script_item_id,
        script_version_id=request.script_version_id,
        actor_id=request.actor_id,
        approval_hash=approval_hash,
        gate_run_id=gate_result.gate_run_id if hasattr(gate_result, "gate_run_id") else "",
        dependencies=dependencies,
    )


def is_approval_fresh(
    approval_hash: str,
    deps: ApprovalDependencies,
) -> bool:
    """True when the approval still binds the current dependency versions.

    Deterministic predicate (task 4.7): recompute the hash over the current
    dependencies and compare. Any text edit, segment version change,
    plan/rule/product-facts/promotion/persona version change stales the
    approval; unrelated metadata never enters the hash, so it cannot stale
    it.
    """
    return approval_hash == approval_dependency_hash(deps)
