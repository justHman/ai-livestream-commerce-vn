"""Sanitized safety counters (task 3.6): reason tallies, never raw text.

``to_dict`` must stay content-safe: it may only ever contain reason-code
keys, integer counts, and totals — never the checked text.
"""

from __future__ import annotations

from backend.application.safety_gate.counters import SafetyCounters
from backend.application.safety_gate.decision import ReasonCode, SafetyDecision


def test_counters_start_empty() -> None:
    assert SafetyCounters().to_dict() == {
        "total_rejected": 0,
        "total_accepted": 0,
    }


def test_counters_accumulate_per_reason() -> None:
    counters = SafetyCounters()
    counters.record(SafetyDecision.reject((ReasonCode.MALFORMED,)))
    counters.record(SafetyDecision.reject((ReasonCode.SPAM,)))
    counters.record(SafetyDecision.reject((ReasonCode.SPAM,)))
    counters.record(SafetyDecision.accept())
    assert counters.to_dict() == {
        "malformed": 1,
        "spam": 2,
        "total_rejected": 3,
        "total_accepted": 1,
    }


def test_counters_record_reject_multiple_codes() -> None:
    counters = SafetyCounters()
    counters.record_reject((ReasonCode.SPAM, ReasonCode.MALFORMED))
    assert counters.to_dict()["spam"] == 1
    assert counters.to_dict()["malformed"] == 1
    assert counters.to_dict()["total_rejected"] == 2


def test_counters_rejected_count_defaults_to_zero() -> None:
    counters = SafetyCounters()
    assert counters.rejected_count(ReasonCode.TOXICITY) == 0


def test_counters_to_dict_never_contains_input_text() -> None:
    counters = SafetyCounters()
    counters.record(SafetyDecision.reject((ReasonCode.PROMPT_INJECTION,)))
    snapshot = counters.to_dict()
    assert "ignore previous" not in str(snapshot)
    assert all(isinstance(v, int) for v in snapshot.values())
