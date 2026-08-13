"""Offline tests for allowlisted evidence ops (tasks 12.7, 12.8).

The executor is the only system boundary; it is faked here because the live
implementation arrives in a later cluster.
"""

from __future__ import annotations

import inspect

import pytest

import backend.application.agentic_director.evidence_ops as evidence_ops_module
from backend.application.agentic_director.evidence_ops import (
    ALLOWED_EVIDENCE_OPS,
    EvidenceOperation,
    EvidenceOperationRejected,
    execute_evidence_operation,
    validate_evidence_operation,
)


class FakeExecutor:
    """Minimal record-call fake of the EvidenceExecutor Protocol."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def search_entities(
        self, queries: tuple[str, ...], entity_type: str | None = None
    ) -> list[dict]:
        self.calls.append(("search_entities", queries, entity_type))
        return [{"entity_id": "sp-123"}]

    def get_entities(
        self, entity_ids: tuple[str, ...], selectors: tuple[str, ...] | None = None
    ) -> list[dict]:
        self.calls.append(("get_entities", entity_ids, selectors))
        return [{"entity_id": entity_ids[0]}]

    def get_evidence(self, requests: tuple) -> list[dict]:
        self.calls.append(("get_evidence", requests))
        return [{"selector": "price", "value": "29.000"}]


def test_allowlist_contains_exactly_the_three_evidence_ops():
    assert ALLOWED_EVIDENCE_OPS == frozenset({"search_entities", "get_entities", "get_evidence"})


def test_validate_accepts_search_entities():
    op = validate_evidence_operation({"op": "search_entities", "queries": ("áo thun",)})
    assert op.op == "search_entities"
    assert op.queries == ("áo thun",)


def test_validate_accepts_get_entities():
    op = validate_evidence_operation(
        {"op": "get_entities", "entity_ids": ("sp-123",), "selectors": ("price",)}
    )
    assert op.entity_ids == ("sp-123",)
    assert op.selectors == ("price",)


def test_validate_accepts_get_evidence():
    op = validate_evidence_operation(
        {"op": "get_evidence", "requests": ({"selector": "price", "entity_id": "sp-123"},)}
    )
    assert op.requests == ({"selector": "price", "entity_id": "sp-123"},)


def test_validate_rejects_read_file_op():
    with pytest.raises(EvidenceOperationRejected) as exc:
        validate_evidence_operation({"op": "read_file", "path": "/etc/passwd"})
    assert exc.value.code == "ev_op_rejected"


def test_validate_rejects_http_get_op():
    with pytest.raises(EvidenceOperationRejected):
        validate_evidence_operation({"op": "http_get", "url": "https://example.com"})


def test_validate_rejects_spawn_job_op():
    with pytest.raises(EvidenceOperationRejected):
        validate_evidence_operation({"op": "spawn_job", "command": "curl"})


def test_validate_rejects_missing_required_args():
    with pytest.raises(EvidenceOperationRejected) as exc:
        validate_evidence_operation({"op": "search_entities"})
    assert "queries" in exc.value.message


def test_validate_rejects_wrong_arg_type():
    with pytest.raises(EvidenceOperationRejected):
        validate_evidence_operation({"op": "search_entities", "queries": "áo thun"})


def test_validate_rejects_unknown_extra_fields():
    with pytest.raises(EvidenceOperationRejected):
        validate_evidence_operation({"op": "get_entities", "entity_ids": ("sp-123",), "offset": 5})


def test_validate_rejects_non_mapping_input():
    with pytest.raises(EvidenceOperationRejected):
        validate_evidence_operation(["search_entities"])


def test_dispatch_executes_search_through_executor():
    executor = FakeExecutor()
    result = execute_evidence_operation(
        executor,
        validate_evidence_operation({"op": "search_entities", "queries": ("áo thun",)}),
    )
    assert result == [{"entity_id": "sp-123"}]
    assert executor.calls == [("search_entities", ("áo thun",), None)]


def test_dispatch_executes_get_entities_through_executor():
    executor = FakeExecutor()
    result = execute_evidence_operation(
        executor,
        validate_evidence_operation(
            {"op": "get_entities", "entity_ids": ("sp-123",), "selectors": ("price",)}
        ),
    )
    assert result == [{"entity_id": "sp-123"}]
    assert executor.calls == [("get_entities", ("sp-123",), ("price",))]


def test_dispatch_executes_get_evidence_through_executor():
    executor = FakeExecutor()
    requests = ({"selector": "price", "entity_id": "sp-123"},)
    result = execute_evidence_operation(
        executor, validate_evidence_operation({"op": "get_evidence", "requests": requests})
    )
    assert result == [{"selector": "price", "value": "29.000"}]
    assert executor.calls == [("get_evidence", requests)]


def test_dispatch_rejects_hand_built_unallowlisted_op():
    with pytest.raises(EvidenceOperationRejected):
        execute_evidence_operation(
            FakeExecutor(),
            EvidenceOperation(op="read_file", queries=("/etc/passwd",)),
        )


def test_evidence_ops_module_exposes_only_the_three_evidence_ops():
    module_public = {
        name
        for name, obj in inspect.getmembers(evidence_ops_module, inspect.isfunction)
        if not name.startswith("_")
        and getattr(obj, "__module__", "") == evidence_ops_module.__name__
    }
    assert module_public == {"validate_evidence_operation", "execute_evidence_operation"}
