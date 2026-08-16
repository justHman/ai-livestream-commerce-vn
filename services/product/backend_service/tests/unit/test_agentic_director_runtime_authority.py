"""Offline tests for the backend runtime authority boundary (task 12.9)."""

from __future__ import annotations

import pytest

from backend.application.agentic_director.runtime_authority import (
    FORBIDDEN_MODEL_INSTRUCTIONS,
    RuntimeAuthority,
    RuntimeInstructionRejected,
    assert_no_model_runtime_authority,
)


class FakeAuthority:
    """Minimal record-call fake of the RuntimeAuthority Protocol."""

    def __init__(self) -> None:
        self.metrics: list[tuple[str, int | float]] = []
        self.revalidations: list[str] = []
        self.budget_checks: list[str] = []

    def record_metric(self, name: str, value: int | float) -> None:
        self.metrics.append((name, value))

    def revalidate_volatile_evidence(self, entity_id: str) -> bool:
        self.revalidations.append(entity_id)
        return True

    def is_within_budget(self, op: str) -> bool:
        self.budget_checks.append(op)
        return True


def test_forbidden_set_contains_scheduling_instruction_classes():
    assert FORBIDDEN_MODEL_INSTRUCTIONS == frozenset(
        {"retry", "pivot", "script_cursor", "job", "interrupt", "schedule"}
    )


def test_clean_evidence_payload_passes():
    assert_no_model_runtime_authority({"op": "search_entities", "queries": ("áo thun",)})


def test_retry_key_raises():
    with pytest.raises(RuntimeInstructionRejected):
        assert_no_model_runtime_authority({"retry": {"attempts": 5}})


def test_pivot_key_raises():
    with pytest.raises(RuntimeInstructionRejected):
        assert_no_model_runtime_authority({"pivot": "product_1"})


def test_script_cursor_key_raises():
    with pytest.raises(RuntimeInstructionRejected):
        assert_no_model_runtime_authority({"script_cursor": {"position": 12}})


def test_job_key_raises():
    with pytest.raises(RuntimeInstructionRejected):
        assert_no_model_runtime_authority({"job": {"name": "apply_coupon"}})


def test_interrupt_key_raises():
    with pytest.raises(RuntimeInstructionRejected):
        assert_no_model_runtime_authority({"interrupt": True})


def test_schedule_key_raises():
    with pytest.raises(RuntimeInstructionRejected):
        assert_no_model_runtime_authority({"schedule": {"at": "12:00"}})


def test_nested_forbidden_key_path_is_detected():
    with pytest.raises(RuntimeInstructionRejected):
        assert_no_model_runtime_authority(
            {"evidence": {"op": "get_evidence", "requests": ({"retry": 3},)}}
        )


def test_error_carries_rt_instr_rejected_code():
    with pytest.raises(RuntimeInstructionRejected) as exc:
        assert_no_model_runtime_authority({"retry": True})
    assert exc.value.code == "rt_instr_rejected"


def test_runtime_authority_protocol_matches_backend_owned_operations():
    assert isinstance(FakeAuthority(), RuntimeAuthority)


def test_authority_exposes_only_backend_owned_operations():
    assert isinstance(FakeAuthority(), RuntimeAuthority)
    # Public surface only: Protocol internals (__protocol_attrs__/__is_protocol__)
    # and get_protocol_members() differ across Python versions (3.11 CI vs 3.14),
    # so dir() is the stable check.
    assert sorted(n for n in dir(RuntimeAuthority) if not n.startswith("_")) == [
        "is_within_budget",
        "record_metric",
        "revalidate_volatile_evidence",
    ]
