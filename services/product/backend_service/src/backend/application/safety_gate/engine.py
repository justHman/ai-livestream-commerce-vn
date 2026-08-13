"""SafetyGate composition engine: checks in order, first rejection wins.

The engine is deliberately small: it runs the deterministic rule families in
curated order (malformed -> replay -> spam), stamps the policy version, and
returns a frozen ``SafetyDecision``. Rejection is first-rejection-wins so the
reason code names the PRIMARY cause, and later checks are skipped once a
rejection is found — cheap and deterministic.

``extra_checks`` lets other clusters plug in rule families (prompt-injection
detection, profanity lexicon) without this package importing their modules:
each entry is a pure ``(text) -> tuple[ReasonCode, ...]`` callable, evaluated
after the built-in families.
"""

from __future__ import annotations

from typing import Callable, Sequence

from .checks import ReplayWindow, check_malformed, check_replay_flood, check_spam
from .decision import ReasonCode, SafetyDecision, SAFETY_POLICY_VERSION

__all__ = ["SafetyGate", "check"]

ExtraCheck = Callable[[str], tuple[ReasonCode, ...]]


def _extract(text: str | None) -> str:
    if text is None:
        return ""
    return text


def check(
    text: str | None,
    *,
    replay_window: ReplayWindow | None = None,
    ts: float = 0.0,
    policy_version: str = SAFETY_POLICY_VERSION,
    extra_checks: Sequence[ExtraCheck] = (),
) -> SafetyDecision:
    """Evaluate one viewer comment through the full gate.

    ``replay_window`` is caller-owned (None disables replay detection);
    ``ts`` must be the caller's monotonic timestamp (explicit so tests stay
    deterministic). ``extra_checks`` receives the ORIGINAL text — the gate
    never persists or forwards rejected text, but a check such as
    prompt-injection detection needs the raw string to see the attack.
    """
    codes = check_malformed(text)
    if codes:
        return SafetyDecision.reject(codes, policy_version=policy_version)
    codes = check_replay_flood(_extract(text), replay_window, ts)
    if codes:
        return SafetyDecision.reject(codes, policy_version=policy_version)
    codes = check_spam(text)
    if codes:
        return SafetyDecision.reject(codes, policy_version=policy_version)
    for extra in extra_checks:
        codes = extra(_extract(text))
        if codes:
            return SafetyDecision.reject(codes, policy_version=policy_version)
    return SafetyDecision.accept(policy_version=policy_version)


class SafetyGate:
    """Thin stateless wrapper so callers can inject the policy version once.

    Kept minimal on purpose: no registry, no rules — the free ``check``
    function is the engine; this class only binds ``policy_version``.
    """

    def __init__(self, policy_version: str = SAFETY_POLICY_VERSION) -> None:
        self._policy_version = policy_version

    def evaluate(
        self,
        text: str | None,
        *,
        replay_window: ReplayWindow | None = None,
        ts: float = 0.0,
        extra_checks: Sequence[ExtraCheck] = (),
    ) -> SafetyDecision:
        """Run the full gate with this instance's policy version."""
        return check(
            text,
            replay_window=replay_window,
            ts=ts,
            policy_version=self._policy_version,
            extra_checks=extra_checks,
        )
