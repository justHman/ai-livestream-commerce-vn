"""Versioned SafetyDecision value object and stable reason codes.

OpenSpec cluster C3 (Safety Gate before embedding, design Decision 3): the
gate's outcome is a frozen value object — ``accepted``, ordered
``reason_codes``, the policy version that produced the verdict, and
content-safe ``sanitized_metrics``. ``reason_codes`` is a tuple because order
matters downstream: the first code names the primary rejection reason.

``ReasonCode`` lives here as the single source of truth. The prompt-injection
check (another cluster's module) imports THIS enum when its check runs; this
package never imports from that module, so the coupling is one-directional.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "ReasonCode",
    "SafetyDecision",
    "SAFETY_POLICY_VERSION",
]

SAFETY_POLICY_VERSION = "1"
"""Curated, versioned rule-set identifier stamped on every decision (task 3.1).

A policy update that changes what content is accepted MUST bump this constant;
persisted audit rows and telemetry keys carry it, so a decision is always
interpretable against the exact rule set that produced it.
"""


class ReasonCode(StrEnum):
    """Stable safety reason codes (never re-purposed; add, don't reuse).

    Every code is produced by exactly one deterministic rule family:
    MALFORMED/REPLAY_FLOOD/SPAM by this package's checks, TOXICITY and the
    remaining codes by checks other clusters contribute (e.g. profanity
    lexicon, LLM-free toxicity heuristics, prompt-injection detector). Codes
    are curated: a new signal only gets a code when a curated rule produces it.
    """

    MALFORMED = "malformed"
    REPLAY_FLOOD = "replay_flood"
    SPAM = "spam"
    PROFANITY = "profanity"
    TOXICITY = "toxicity"
    HARASSMENT = "harassment"
    UNSAFE_CONTENT = "unsafe_content"
    PROMPT_INJECTION = "prompt_injection"


@dataclass(frozen=True)
class SafetyDecision:
    """Frozen verdict of one Safety Gate evaluation (design Decision 3).

    ``reason_codes`` is ordered: the first code is the primary reason, in
    evaluation order (malformed -> replay -> spam -> extra checks). An
    accepted decision carries empty ``reason_codes``. ``sanitized_metrics``
    holds content-safe strings only — never raw viewer text (design
    Observability section forbids private text in generic telemetry).
    """

    accepted: bool
    reason_codes: tuple[ReasonCode, ...] = ()
    policy_version: str = SAFETY_POLICY_VERSION
    sanitized_metrics: tuple[str, ...] = ()

    @property
    def rejected(self) -> bool:
        """Inverse of ``accepted``; rejection = non-empty reason codes."""
        return not self.accepted

    @classmethod
    def reject(
        cls,
        reason_codes: tuple[ReasonCode, ...],
        policy_version: str = SAFETY_POLICY_VERSION,
        sanitized_metrics: tuple[str, ...] = (),
    ) -> SafetyDecision:
        """Factory for a rejected decision; the rejected flag is not optional.

        Callers must never construct ``SafetyDecision(accepted=False, ...)``
        with empty reason codes by hand — the factory keeps that invariant in
        one place. ``reason_codes`` is normalized to a tuple so a frozen,
        hashable value object never holds a mutable list.
        """
        return cls(
            accepted=False,
            reason_codes=tuple(reason_codes),
            policy_version=policy_version,
            sanitized_metrics=sanitized_metrics,
        )

    @classmethod
    def accept(
        cls,
        policy_version: str = SAFETY_POLICY_VERSION,
        sanitized_metrics: tuple[str, ...] = (),
    ) -> SafetyDecision:
        """Factory for an accepted decision."""
        return cls(
            accepted=True,
            policy_version=policy_version,
            sanitized_metrics=sanitized_metrics,
        )
