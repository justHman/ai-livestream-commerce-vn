"""Versioned ``ScriptRuleRegistry`` — one canonical source of rule truth.

Task 3.1 / design Decision 3: every deterministic gate rule is a versioned
object with a stable ID and three consumers — the gate checker itself, the
generation prompt builder (``generation_constraint``), and the repair prompt
builder (``repair_instruction``). Prompt builders read THIS registry; they
never maintain a hand-copied rules document.

Rule IDs use the design's stable families: ``FORMAT_*``, ``STYLE_*``,
``VN_SPELLING_*``, ``PROFANITY_*``, ``CLAIM_*``, ``TTS_*``,
``REPETITION_*``, ``SPEECH_DURATION_*``, plus full-script families
``COVERAGE_*``, ``CTA_*``, ``TONE_*``, ``TRANSITION_*``.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from .results import Severity

__all__ = [
    "RuleSpec",
    "ScriptRuleRegistry",
    "RuleNotFoundError",
    "validate_rule_id",
]

# Stable rule-ID namespace (task 3.1: stable versioned metadata). New rules
# MUST be registered under one of these families and keep their ID forever;
# a changed rule bumps its version, never its ID.
_RULE_ID_RE = re.compile(
    r"^(?:FORMAT|STYLE|VN_SPELLING|PROFANITY|CLAIM|TTS|REPETITION|"
    r"SPEECH_DURATION|COVERAGE|CTA|TONE|TRANSITION)_[A-Z0-9_]+$"
)


def validate_rule_id(rule_id: str) -> None:
    if not _RULE_ID_RE.match(rule_id):
        raise ValueError(
            f"invalid rule id {rule_id!r}: must match "
            f"{_RULE_ID_RE.pattern} with a documented family prefix"
        )


@dataclass(frozen=True)
class RuleSpec:
    """One versioned rule with its deterministic checker and consumer texts.

    ``check`` is pure: given the segment/full-script context it returns
    zero or more violations with THIS rule's ID. ``user_message`` is shown to
    humans; ``generation_constraint`` is injected into Generate prompts; only
    ``repair_instruction`` is injected into Fix prompts for this exact rule.
    """

    id: str
    version: int
    severity: Severity
    check: Callable[..., list]
    user_message: str
    generation_constraint: str = ""
    repair_instruction: str = ""

    def __post_init__(self) -> None:
        validate_rule_id(self.id)
        if self.version < 1:
            raise ValueError(f"rule {self.id}: version must be >= 1")


class RuleNotFoundError(KeyError):
    """Raised when a rule ID is not registered (repair/generation builders)."""


class ScriptRuleRegistry:
    """Canonical versioned registry of gate rules.

    Immutable after construction: rules register once at module import time,
    then are looked up by ID or iterated in stable (registration) order.
    """

    def __init__(self, rules: list[RuleSpec]) -> None:
        seen: dict[str, RuleSpec] = {}
        for rule in rules:
            if rule.id in seen:
                raise ValueError(f"duplicate rule id {rule.id!r}")
            seen[rule.id] = rule
        self._rules: dict[str, RuleSpec] = dict(seen)
        self._ordered: list[RuleSpec] = list(rules)

    def get(self, rule_id: str) -> RuleSpec:
        """Look up one rule; raises ``RuleNotFoundError`` when unknown."""
        try:
            return self._rules[rule_id]
        except KeyError as exc:
            raise RuleNotFoundError(rule_id) from exc

    def __getitem__(self, rule_id: str) -> RuleSpec:
        return self.get(rule_id)

    def __contains__(self, rule_id: str) -> bool:
        return rule_id in self._rules

    def __iter__(self):
        return iter(self._ordered)

    def __len__(self) -> int:
        return len(self._rules)

    def ids(self) -> list[str]:
        return [rule.id for rule in self._ordered]

    def version(self, rule_id: str) -> int:
        return self.get(rule_id).version

    def versions(self) -> list[tuple[str, int]]:
        """Sorted ``(rule_id, version)`` pairs for fingerprints (task 3.2)."""
        return sorted((rule.id, rule.version) for rule in self._ordered)

    def generation_constraints(self, rule_ids: list[str]) -> list[str]:
        """``generation_constraint`` text for the given rules, in registry order.

        Used by the Generate prompt builder (spec: generation uses registry
        constraints). Unknown IDs fail loudly rather than silently dropping a
        constraint the caller believes exists.
        """
        return [
            self.get(rule_id).generation_constraint
            for rule_id in rule_ids
            if self.get(rule_id).generation_constraint
        ]

    def repair_instructions(self, rule_ids: list[str]) -> list[str]:
        """``repair_instruction`` text for the given rules, in registry order.

        Used by the Fix prompt builder: only the exact failed rules' repair
        instructions are injected (spec: repair uses only failed rules).
        """
        return [
            self.get(rule_id).repair_instruction
            for rule_id in rule_ids
            if self.get(rule_id).repair_instruction
        ]
