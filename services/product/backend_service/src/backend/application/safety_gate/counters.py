"""Sanitized safety counters: reason-code tallies, never raw text.

Task 3.6 / design Observability section: rejection counters must be
observable, but raw private viewer text MUST NOT enter generic telemetry.
``SafetyCounters`` therefore only ever records reason codes and integer
counts; ``to_dict()`` returns a plain serializable dict. No lock: ingestion is
per-session single-threaded by design; if that ever changes, add a lock here
rather than in callers.
"""

from __future__ import annotations

from .decision import ReasonCode

__all__ = ["SafetyCounters"]


class SafetyCounters:
    """Per-reason rejection counters plus accepted/rejected totals.

    Records are idempotent dict increments; ``to_dict`` keys are stable
    reason-code values, so telemetry can bind counts to the exact codes
    without ever seeing the text that produced them.
    """

    def __init__(self) -> None:
        self._rejected: dict[str, int] = {}
        self._total_accepted = 0

    def record(self, decision) -> None:
        """Accumulate one decision: totals always, per-code only on reject."""
        if decision.accepted:
            self._total_accepted += 1
            return
        for code in decision.reason_codes:
            key = str(code)
            self._rejected[key] = self._rejected.get(key, 0) + 1

    def record_reject(self, reason_codes) -> None:
        """Accumulate one rejection with its (possibly multiple) codes."""
        for code in reason_codes:
            key = str(code)
            self._rejected[key] = self._rejected.get(key, 0) + 1

    def rejected_count(self, code: ReasonCode) -> int:
        """Count for one reason code (0 when never recorded)."""
        return self._rejected.get(str(code), 0)

    def to_dict(self) -> dict[str, int]:
        """Content-safe serializable snapshot: reason -> count, plus totals."""
        data: dict[str, int] = dict(self._rejected)
        data["total_rejected"] = sum(self._rejected.values())
        data["total_accepted"] = self._total_accepted
        return data
