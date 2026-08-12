"""Gate result types with stable rule IDs, severity, and rule-set fingerprint.

Task 3.2: a gate run produces a deterministic ``GateRunResult`` carrying
stable rule IDs (never free-form messages), severity, an optional text span,
the implicated segment IDs at full-script scope, and a rule-set fingerprint so
downstream consumers (approval hashes, repair prompts, telemetry) can bind to
the exact rule versions that produced the run.

Violations are value objects: frozen, hashable, and comparable so two runs
over identical input/context/rule versions produce identical results.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import hashlib

__all__ = [
    "Severity",
    "TextSpan",
    "RuleViolation",
    "RuleSetFingerprint",
    "GateRunResult",
]


class Severity(StrEnum):
    """Deterministic gate severity levels.

    ERROR blocks a script (gate FAIL); WARNING never blocks but is surfaced
    for human review. Severity is rule metadata, not a runtime decision —
    the gate never softens an ERROR into a warning.
    """

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class TextSpan:
    """Zero-based half-open span ``[start, end)`` into the checked text.

    Optional per violation; spans never cross a segment boundary.
    """

    start: int
    end: int


@dataclass(frozen=True)
class RuleViolation:
    """One deterministic policy violation.

    ``rule_id`` and ``severity`` are the stable identifiers; ``message`` is
    the user-facing text. ``segment_index`` is set only by full-script rules
    that implicate a specific segment (cross-segment repetition,
    contradictory claims); ``text_span`` optionally points into the checked
    text (segment scope) or the segment text (full-script scope).
    """

    rule_id: str
    severity: Severity
    message: str
    segment_index: int | None = None
    text_span: TextSpan | None = None


@dataclass(frozen=True)
class RuleSetFingerprint:
    """Fingerprint over the exact rule versions that produced a gate run.

    Computed from the registry's sorted ``(rule_id, version)`` pairs of the
    rules that were actually evaluated, so any bound rule-version change is
    visible to approval-hash consumers (task 4.4) without string munging.
    """

    rule_ids: tuple[tuple[str, int], ...] = field(default_factory=tuple)

    @classmethod
    def from_rule_versions(cls, rule_versions: list[tuple[str, int]]) -> RuleSetFingerprint:
        return cls(tuple(sorted(rule_versions)))

    @property
    def hexdigest(self) -> str:
        digest = hashlib.sha256()
        for rule_id, version in self.rule_ids:
            digest.update(f"{rule_id}:{version}\n".encode("utf-8"))
        return digest.hexdigest()[:16]

    def __contains__(self, rule_id: str) -> bool:
        return any(rid == rule_id for rid, _ in self.rule_ids)


@dataclass(frozen=True)
class GateRunResult:
    """Deterministic outcome of one gate run at segment or full-script scope.

    ``passed`` is derived: no ERROR violations. WARNINGs never fail a run.
    ``scope`` distinguishes ``segment`` from ``full_script`` so consumers can
    distinguish the two gate kinds without inferring it from the rules.
    """

    scope: str
    violations: tuple[RuleViolation, ...] = ()
    fingerprint: RuleSetFingerprint = field(default_factory=RuleSetFingerprint)

    @property
    def passed(self) -> bool:
        return not any(v.severity is Severity.ERROR for v in self.violations)

    @property
    def errors(self) -> tuple[RuleViolation, ...]:
        return tuple(v for v in self.violations if v.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[RuleViolation, ...]:
        return tuple(v for v in self.violations if v.severity is Severity.WARNING)

    def by_rule_id(self, rule_id: str) -> tuple[RuleViolation, ...]:
        """Violations of one stable rule ID (used by repair-prompt builders)."""
        return tuple(v for v in self.violations if v.rule_id == rule_id)
