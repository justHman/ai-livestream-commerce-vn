"""Deterministic ScriptGate package (OpenSpec cluster 3, tasks 3.1-3.12).

PURE gate: no LLM, no network. The canonical ``ScriptRuleRegistry`` supplies
rule metadata to the gate and to generation/repair prompt builders; the
``ScriptGate`` engine runs segment and full-script rule sets and produces
deterministic ``GateRunResult`` values. Speech-duration checks reuse Change
A's ``backend.application.text_chunker.SpeechDurationEstimator``.
"""

from __future__ import annotations

from .context import ProductFacts, ScriptGateContext
from .engine import (
    FullScriptRuleSet,
    ScriptGate,
    SegmentRuleSet,
    default_full_script_rules,
    default_segment_rules,
)
from .registry import RuleNotFoundError, RuleSpec, ScriptRuleRegistry
from .results import (
    GateRunResult,
    RuleSetFingerprint,
    RuleViolation,
    Severity,
    TextSpan,
)

__all__ = [
    "ProductFacts",
    "ScriptGateContext",
    "ScriptGate",
    "SegmentRuleSet",
    "FullScriptRuleSet",
    "default_segment_rules",
    "default_full_script_rules",
    "ScriptRuleRegistry",
    "RuleSpec",
    "RuleNotFoundError",
    "GateRunResult",
    "RuleSetFingerprint",
    "RuleViolation",
    "Severity",
    "TextSpan",
]
