"""Deterministic ``ScriptGate`` — the pass/fail authority (tasks 3.9-3.12).

The gate runs the registered rule set against a segment or a compiled full
script and produces a ``GateRunResult``. It is the single authority for
policy pass/fail: LLM self-assessment never replaces it, and the API maps a
gate FAIL to the stable domain ``gate_failed`` state (never a transport
error).

Pure by construction: no LLM, no network, no filesystem beyond the packaged
profanity lexicon. Identical content + identical context + identical rule
versions => identical result (task 3.12 contract).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .context import ScriptGateContext
from .registry import RuleSpec, ScriptRuleRegistry
from .results import GateRunResult, RuleSetFingerprint, RuleViolation
from .rules import (
    check_common_spelling,
    check_contradictory_claims,
    check_control_characters,
    check_cross_segment_repetition,
    check_cta_frequency,
    check_cta_pacing,
    check_discount_claims,
    check_em_dash,
    check_factual_claims,
    check_identity_claims,
    check_local_repetition,
    check_price_claims,
    check_profanity,
    check_punctuation,
    check_required_coverage,
    check_segment_duration,
    check_tense_spacing,
    check_tone_consistency,
    check_total_duration,
    check_transition_policy,
    check_tts_acronyms,
    check_tts_control_chars,
    check_tts_markup,
    check_tts_numbers,
    check_whitespace,
)

__all__ = [
    "ScriptGate",
    "SegmentRuleSet",
    "FullScriptRuleSet",
    "default_segment_rules",
    "default_full_script_rules",
]


@dataclass(frozen=True)
class SegmentRuleSet:
    """Rules evaluated at SEGMENT scope (tasks 3.3-3.9).

    The list is ordered (registry iteration order) and every rule is
    segment-local: format, Vietnamese spelling, profanity, claims, TTS
    readiness, local repetition (task 3.9), and target duration (task 3.9,
    via the Change A ``SpeechDurationEstimator``).
    """

    rules: list[RuleSpec] = field(default_factory=list)

    def __iter__(self):
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)


@dataclass(frozen=True)
class FullScriptRuleSet:
    """Rules evaluated at FULL-SCRIPT scope (task 3.10).

    Cross-segment rules: cross-segment repetition, contradictory claims,
    required coverage, CTA pacing, tone/persona consistency, transition
    policy, and overall spoken duration.
    """

    rules: list[RuleSpec] = field(default_factory=list)

    def __iter__(self):
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)


class ScriptGate:
    """Deterministic gate runner bound to a registry.

    ``run_segment(text, context)`` evaluates the segment rule set;
    ``run_full_script(segments, context)`` evaluates the full-script rule
    set over a list of segment texts (the exact selected segment versions).
    Both return a ``GateRunResult`` with a rule-set fingerprint.
    """

    def __init__(
        self,
        registry: ScriptRuleRegistry,
        segment_rules: SegmentRuleSet,
        full_rules: FullScriptRuleSet,
    ) -> None:
        self._registry = registry
        self._segment_rules = segment_rules
        self._full_rules = full_rules

    # -- public API --------------------------------------------------------

    def run_segment(self, text: str, context: ScriptGateContext) -> GateRunResult:
        """Evaluate one segment's text at segment scope."""
        violations: list[RuleViolation] = []
        for rule in self._segment_rules:
            violations.extend(rule.check(text, context))
        fingerprint = RuleSetFingerprint.from_rule_versions(
            [(rule.id, rule.version) for rule in self._segment_rules]
        )
        return GateRunResult(scope="segment", violations=tuple(violations), fingerprint=fingerprint)

    def run_full_script(
        self,
        segments: list[str],
        context: ScriptGateContext,
    ) -> GateRunResult:
        """Evaluate a compiled full script (exact ordered segment texts).

        ``segments`` must be the exact selected segment versions in order;
        the full-script rules receive the whole list so cross-segment
        checks can attribute violations to ``segment_index``.
        """
        violations: list[RuleViolation] = []
        for rule in self._full_rules:
            violations.extend(rule.check(segments, context))
        fingerprint = RuleSetFingerprint.from_rule_versions(
            [(rule.id, rule.version) for rule in self._full_rules]
        )
        return GateRunResult(scope="full_script", violations=tuple(violations), fingerprint=fingerprint)

    # -- registry access for prompt builders -------------------------------

    def registry(self) -> ScriptRuleRegistry:
        """The canonical registry (generation/repair builders read it)."""
        return self._registry


def default_segment_rules() -> SegmentRuleSet:
    """The canonical segment rule set in evaluation order."""
    return SegmentRuleSet(
        rules=[
            RuleSpec(
                id="FORMAT_CONTROL",
                version=1,
                severity="error",
                check=check_control_characters,
                user_message="Hidden control characters found.",
                generation_constraint="Do not use hidden control characters.",
                repair_instruction="Remove hidden control characters.",
            ),
            RuleSpec(
                id="FORMAT_WHITESPACE",
                version=1,
                severity="warning",
                check=check_whitespace,
                user_message="Whitespace hygiene issue.",
                generation_constraint="Use single spaces; no tabs.",
                repair_instruction="Collapse whitespace to single spaces.",
            ),
            RuleSpec(
                id="FORMAT_PUNCTUATION",
                version=1,
                severity="error",
                check=check_punctuation,
                user_message="Repeated/malformed punctuation.",
                generation_constraint="Use normal punctuation; no repeated !? runs.",
                repair_instruction="Replace repeated punctuation with a single mark.",
            ),
            RuleSpec(
                id="STYLE_EM_DASH",
                version=1,
                severity="error",
                check=check_em_dash,
                user_message="Em/en dash used; house style forbids it.",
                generation_constraint="Never use em-dashes or en-dashes.",
                repair_instruction="Replace em/en dashes with commas or periods.",
            ),
            RuleSpec(
                id="VN_SPELLING_TONE",
                version=1,
                severity="warning",
                check=check_tense_spacing,
                user_message="Tone-mark spelling issue.",
                generation_constraint="Write Vietnamese with correct tone marks.",
                repair_instruction="Fix tone-mark placement.",
            ),
            RuleSpec(
                id="VN_SPELLING_GI_D",
                version=1,
                severity="error",
                check=check_common_spelling,
                user_message="Common Vietnamese spelling confusion.",
                generation_constraint="Use correct Vietnamese spelling (gi vs d).",
                repair_instruction="Fix the gi/d spelling.",
            ),
            RuleSpec(
                id="PROFANITY_OFFENSIVE",
                version=1,
                severity="error",
                check=check_profanity,
                user_message="Offensive language found.",
                generation_constraint="Never use profanity or offensive language.",
                repair_instruction="Remove the offensive language.",
            ),
            RuleSpec(
                id="CLAIM_PRICE",
                version=1,
                severity="error",
                check=check_price_claims,
                user_message="Unverified price claim.",
                generation_constraint="Only use the authoritative prices.",
                repair_instruction="Use the authoritative price.",
            ),
            RuleSpec(
                id="CLAIM_DISCOUNT",
                version=1,
                severity="error",
                check=check_discount_claims,
                user_message="Unverified discount claim.",
                generation_constraint="Only use the authoritative discounts.",
                repair_instruction="Use the authoritative discount.",
            ),
            RuleSpec(
                id="CLAIM_IDENTITY",
                version=1,
                severity="error",
                check=check_identity_claims,
                user_message="Unverified product/SKU identity.",
                generation_constraint="Only reference authoritative SKUs and the product name.",
                repair_instruction="Use the authoritative SKU/product name.",
            ),
            RuleSpec(
                id="CLAIM_FACTUAL",
                version=1,
                severity="error",
                check=check_factual_claims,
                user_message="Unverified factual claim.",
                generation_constraint="Only state allowed factual claims.",
                repair_instruction="Restate the claim to match the allowed claims.",
            ),
            RuleSpec(
                id="TTS_NUMBER",
                version=1,
                severity="warning",
                check=check_tts_numbers,
                user_message="Number should be verbalized for TTS.",
                generation_constraint="Write numbers in a speakable form.",
                repair_instruction="Verbalize the number for TTS.",
            ),
            RuleSpec(
                id="TTS_MARKUP",
                version=1,
                severity="error",
                check=check_tts_markup,
                user_message="Markup found in spoken text.",
                generation_constraint="No markup in script text.",
                repair_instruction="Remove markup from the text.",
            ),
            RuleSpec(
                id="TTS_CONTROL",
                version=1,
                severity="error",
                check=check_tts_control_chars,
                user_message="Hidden control character found.",
                generation_constraint="No control characters.",
                repair_instruction="Remove control characters.",
            ),
            RuleSpec(
                id="TTS_ACRONYM",
                version=1,
                severity="warning",
                check=check_tts_acronyms,
                user_message="Acronym may be mispronounced.",
                generation_constraint="Spell acronyms in a speakable form.",
                repair_instruction="Confirm/expand the acronym's spoken form.",
            ),
            RuleSpec(
                id="REPETITION_LOCAL",
                version=1,
                severity="error",
                check=check_local_repetition,
                user_message="Phrase repeated within this segment.",
                generation_constraint="Vary wording; do not repeat phrases.",
                repair_instruction="Vary the repeated phrase.",
            ),
            RuleSpec(
                id="REPETITION_CTA",
                version=1,
                severity="error",
                check=check_cta_frequency,
                user_message="Too many CTAs in this segment.",
                generation_constraint="Use at most the allowed CTA count.",
                repair_instruction="Reduce CTAs to the allowed count.",
            ),
            RuleSpec(
                id="SPEECH_DURATION_SEGMENT",
                version=1,
                severity="error",
                check=check_segment_duration,
                user_message="Segment spoken duration out of target range.",
                generation_constraint="Write for the target spoken duration.",
                repair_instruction="Adjust length to hit the target duration.",
            ),
        ]
    )


def default_full_script_rules() -> FullScriptRuleSet:
    """The canonical full-script rule set in evaluation order (task 3.10)."""
    return FullScriptRuleSet(
        rules=[
            RuleSpec(
                id="REPETITION_CROSS",
                version=1,
                severity="error",
                check=check_cross_segment_repetition,
                user_message="Phrase repeated across segments.",
                generation_constraint="Distribute content; no cross-segment repetition.",
                repair_instruction="Move the repeated content to one segment.",
            ),
            RuleSpec(
                id="CLAIM_CONTRADICTION",
                version=1,
                severity="error",
                check=check_contradictory_claims,
                user_message="Contradictory claims across segments.",
                generation_constraint="Keep claims consistent across segments.",
                repair_instruction="Align the contradictory claim with the other segment.",
            ),
            RuleSpec(
                id="COVERAGE_REQUIRED",
                version=1,
                severity="error",
                check=check_required_coverage,
                user_message="Required topic not covered.",
                generation_constraint="Cover every required topic across the script.",
                repair_instruction="Add the missing required topic.",
            ),
            RuleSpec(
                id="CTA_PACING",
                version=1,
                severity="error",
                check=check_cta_pacing,
                user_message="Too many CTAs across the script.",
                generation_constraint="Keep CTA pacing within the per-segment limit.",
                repair_instruction="Reduce CTAs to the pacing limit.",
            ),
            RuleSpec(
                id="TONE_CONSISTENCY",
                version=1,
                severity="warning",
                check=check_tone_consistency,
                user_message="Tone/persona consistency signal.",
                generation_constraint="Keep a calm selling tone across segments.",
                repair_instruction="Softening the tone in the flagged segment.",
            ),
            RuleSpec(
                id="TRANSITION_ORDER",
                version=1,
                severity="error",
                check=check_transition_policy,
                user_message="ORDER_AGNOSTIC script mentions another product.",
                generation_constraint="Never mention other products in ORDER_AGNOSTIC scripts.",
                repair_instruction="Remove the other-product reference.",
            ),
            RuleSpec(
                id="SPEECH_DURATION_TOTAL",
                version=1,
                severity="error",
                check=check_total_duration,
                user_message="Total spoken duration out of range.",
                generation_constraint="Hit the total target spoken duration.",
                repair_instruction="Adjust segment lengths to hit the total duration.",
            ),
        ]
    )
