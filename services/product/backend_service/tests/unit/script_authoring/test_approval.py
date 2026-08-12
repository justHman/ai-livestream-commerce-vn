"""Tasks 4.6/4.7 tests: human-only, dependency-bound approval.

A gate-passed script remains REVIEWABLE until an authorized human approves
it; a stale approval (any bound dependency changed after approval) cannot
bind to runtime via ``is_approval_fresh``.
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.approval import (
    ApprovalError,
    ApprovalRequest,
    approve_script,
    is_approval_fresh,
)
from backend.application.script_authoring.fingerprints import (
    ApprovalDependencies,
)
from backend.application.script_authoring.gate.results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
)


class _GateStub:
    """GateChecker stub: returns the configured full-script result."""

    def __init__(self, result: GateRunResult) -> None:
        self._result = result
        self.calls: list[str] = []

    def check_full_script(self, compiled_spoken_text: str) -> GateRunResult:
        self.calls.append(compiled_spoken_text)
        return self._result


def _passing_gate() -> GateRunResult:
    return GateRunResult(
        scope="full_script",
        violations=(),
        fingerprint=RuleSetFingerprint.from_rule_versions(
            [("CLAIM_PRICE", 2), ("FORMAT_CONTROL", 1)]
        ),
    )


def _failing_gate() -> GateRunResult:
    return GateRunResult(
        scope="full_script",
        violations=(
            RuleViolation(
                rule_id="CLAIM_PRICE",
                severity=Severity.ERROR,
                message="Unverified price claim.",
            ),
        ),
        fingerprint=RuleSetFingerprint.from_rule_versions(
            [("CLAIM_PRICE", 2), ("FORMAT_CONTROL", 1)]
        ),
    )


def _request(**overrides) -> ApprovalRequest:
    base = dict(
        script_item_id="script_item:abc",
        script_version_id="script_version:1",
        compiled_spoken_text="Kem ABC chỉ hai trăm chín mươi chín nghìn đồng.",
        segment_version_ids=("segment:1", "segment:2"),
        plan_version=3,
        rule_set_key="CLAIM_PRICE:2,FORMAT_CONTROL:1",
        product_facts_version="facts-v5",
        promotion_version="promo-v2",
        persona_brief_version="persona-v1",
        actor_id="user-42",
        is_human=True,
        authorized=True,
    )
    base.update(overrides)
    return ApprovalRequest(**base)


def test_gate_pass_is_not_approval_without_human() -> None:
    """A gate-passed script stays non-approved when the actor is not human."""
    gate = _GateStub(_passing_gate())
    with pytest.raises(ApprovalError, match="authenticated human"):
        approve_script(
            _request(is_human=False),
            gate_checker=gate,
            new_approval_id="approval:1",
        )
    assert gate.calls == []  # refused before even checking the gate


def test_unauthorized_actor_refused() -> None:
    gate = _GateStub(_passing_gate())
    with pytest.raises(ApprovalError, match="not authorized"):
        approve_script(
            _request(authorized=False),
            gate_checker=gate,
            new_approval_id="approval:1",
        )
    assert gate.calls == []


def test_human_approves_gate_passed_version() -> None:
    gate = _GateStub(_passing_gate())
    record = approve_script(
        _request(),
        gate_checker=gate,
        new_approval_id="approval:9",
    )
    assert record.id == "approval:9"
    assert record.script_item_id == "script_item:abc"
    assert record.script_version_id == "script_version:1"
    assert record.actor_id == "user-42"
    assert len(record.approval_hash) == 64
    # The gate was checked against the EXACT compiled spoken text.
    assert gate.calls == ["Kem ABC chỉ hai trăm chín mươi chín nghìn đồng."]


def test_gate_fail_refuses_approval() -> None:
    gate = _GateStub(_failing_gate())
    with pytest.raises(ApprovalError, match="did not pass"):
        approve_script(
            _request(),
            gate_checker=gate,
            new_approval_id="approval:1",
        )


def test_wrong_gate_scope_refused() -> None:
    segment_result = GateRunResult(scope="segment", violations=())
    gate = _GateStub(segment_result)
    with pytest.raises(ApprovalError, match="full_script"):
        approve_script(
            _request(),
            gate_checker=gate,
            new_approval_id="approval:1",
        )


def test_stale_approval_cannot_bind_to_runtime() -> None:
    """Promotion changes after approval -> the approval is not fresh."""
    gate = _GateStub(_passing_gate())
    record = approve_script(
        _request(),
        gate_checker=gate,
        new_approval_id="approval:1",
    )

    current = ApprovalDependencies(
        spoken_text=record.dependencies.spoken_text,
        segment_hashes=record.dependencies.segment_hashes,
        plan_version=record.dependencies.plan_version,
        rule_set=record.dependencies.rule_set,
        product_facts_version=record.dependencies.product_facts_version,
        # The authoritative promotion was re-versioned after approval.
        promotion_version="promo-v3",
        persona_brief_version=record.dependencies.persona_brief_version,
    )
    assert not is_approval_fresh(record.approval_hash, current)


def test_fresh_approval_binds() -> None:
    gate = _GateStub(_passing_gate())
    record = approve_script(
        _request(),
        gate_checker=gate,
        new_approval_id="approval:1",
    )
    assert is_approval_fresh(record.approval_hash, record.dependencies)


def test_text_edit_stales_approval() -> None:
    gate = _GateStub(_passing_gate())
    record = approve_script(
        _request(),
        gate_checker=gate,
        new_approval_id="approval:1",
    )
    edited = ApprovalDependencies(
        spoken_text="Kem ABC chỉ hai trăm chín mươi chín nghìn đồng!",
        segment_hashes=record.dependencies.segment_hashes,
        plan_version=record.dependencies.plan_version,
        rule_set=record.dependencies.rule_set,
        product_facts_version=record.dependencies.product_facts_version,
        promotion_version=record.dependencies.promotion_version,
        persona_brief_version=record.dependencies.persona_brief_version,
    )
    assert not is_approval_fresh(record.approval_hash, edited)


def test_prior_approval_does_not_transfer_to_new_version() -> None:
    """Editing creates a new version; the old approval does not follow."""
    gate = _GateStub(_passing_gate())
    old = approve_script(
        _request(),
        gate_checker=gate,
        new_approval_id="approval:1",
    )
    # A text edit creates a NEW version -> the old approval is stale for it.
    new_version_deps = ApprovalDependencies(
        spoken_text="Kem ABC chỉ hai trăm chín mươi chín nghìn đồng mới.",
        segment_hashes=old.dependencies.segment_hashes,
        plan_version=old.dependencies.plan_version,
        rule_set=old.dependencies.rule_set,
        product_facts_version=old.dependencies.product_facts_version,
        promotion_version=old.dependencies.promotion_version,
        persona_brief_version=old.dependencies.persona_brief_version,
    )
    assert not is_approval_fresh(old.approval_hash, new_version_deps)
