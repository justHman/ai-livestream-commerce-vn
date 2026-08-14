"""Deterministic Safety Gate package (OpenSpec cluster C3, tasks 3.1-3.6).

PURE gate: no LLM, no network, no real clock. ``check`` runs the built-in
malformed/replay-flood/spam rule families in order and returns a frozen,
policy-versioned ``SafetyDecision``; ``extra_checks`` lets other clusters add
rule families (prompt-injection, profanity) without import coupling.
``SafetyCounters`` exposes content-safe rejection tallies for telemetry —
raw viewer text never leaves this boundary.
"""

from __future__ import annotations

from .checks import MAX_TEXT_LENGTH, REPLAY_WINDOW_SECONDS, ReplayWindow
from .counters import SafetyCounters
from .decision import (
    ReasonCode,
    SafetyDecision,
    SAFETY_POLICY_VERSION,
)
from .engine import ExtraCheck, SafetyGate, check

__all__ = [
    "ReasonCode",
    "SafetyDecision",
    "SAFETY_POLICY_VERSION",
    "ReplayWindow",
    "MAX_TEXT_LENGTH",
    "REPLAY_WINDOW_SECONDS",
    "SafetyCounters",
    "SafetyGate",
    "check",
    "ExtraCheck",
]
