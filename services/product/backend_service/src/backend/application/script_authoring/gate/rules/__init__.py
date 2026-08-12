"""Deterministic gate rule families (tasks 3.3-3.10).

Every rule is a pure ``check(text_or_segments, context) -> list[RuleViolation]``
function with a stable ID registered in the canonical ``ScriptRuleRegistry``
(see gate.registry). No rule touches the network or an LLM.
"""

from __future__ import annotations

from .commerce_claims import (
    RULE_CLAIM_DISCOUNT,
    RULE_CLAIM_FACTUAL,
    RULE_CLAIM_IDENTITY,
    RULE_CLAIM_PRICE,
    check_discount_claims,
    check_factual_claims,
    check_identity_claims,
    check_price_claims,
)
from .duration import RULE_SPEECH_DURATION, check_segment_duration
from .format import (
    RULE_FORMAT_CONTROL,
    RULE_FORMAT_PUNCTUATION,
    RULE_FORMAT_WHITESPACE,
    RULE_STYLE_EM_DASH,
    check_control_characters,
    check_em_dash,
    check_punctuation,
    check_whitespace,
)
from .full_script import (
    RULE_CLAIM_CONTRADICTION,
    RULE_COVERAGE_REQUIRED,
    RULE_CTA_PACING,
    RULE_REPETITION_CROSS,
    RULE_SPEECH_DURATION_TOTAL,
    RULE_TONE_CONSISTENCY,
    RULE_TRANSITION_ORDER,
    check_contradictory_claims,
    check_cross_segment_repetition,
    check_cta_pacing,
    check_required_coverage,
    check_tone_consistency,
    check_total_duration,
    check_transition_policy,
)
from .profanity import (
    RULE_PROFANITY_OFFENSIVE,
    ProfanityLexicon,
    check_profanity,
    load_curated_lexicon,
)
from .repetition import (
    RULE_REPETITION_CTA,
    RULE_REPETITION_LOCAL,
    check_cta_frequency,
    check_local_repetition,
)
from .tts_readiness import (
    RULE_TTS_ACRONYM,
    RULE_TTS_CONTROL,
    RULE_TTS_MARKUP,
    RULE_TTS_NUMBER,
    check_tts_acronyms,
    check_tts_control_chars,
    check_tts_markup,
    check_tts_numbers,
    normalize_tts_text,
)
from .vietnamese import (
    RULE_VN_SPELLING_GI_D,
    RULE_VN_SPELLING_TONE,
    check_common_spelling,
    check_tense_spacing,
)

__all__ = [
    # format
    "RULE_FORMAT_CONTROL",
    "RULE_FORMAT_WHITESPACE",
    "RULE_FORMAT_PUNCTUATION",
    "RULE_STYLE_EM_DASH",
    "check_control_characters",
    "check_whitespace",
    "check_punctuation",
    "check_em_dash",
    # vietnamese
    "RULE_VN_SPELLING_TONE",
    "RULE_VN_SPELLING_GI_D",
    "check_tense_spacing",
    "check_common_spelling",
    # profanity
    "RULE_PROFANITY_OFFENSIVE",
    "ProfanityLexicon",
    "check_profanity",
    "load_curated_lexicon",
    # commerce claims
    "RULE_CLAIM_PRICE",
    "RULE_CLAIM_DISCOUNT",
    "RULE_CLAIM_IDENTITY",
    "RULE_CLAIM_FACTUAL",
    "check_price_claims",
    "check_discount_claims",
    "check_identity_claims",
    "check_factual_claims",
    # tts readiness
    "RULE_TTS_NUMBER",
    "RULE_TTS_MARKUP",
    "RULE_TTS_CONTROL",
    "RULE_TTS_ACRONYM",
    "check_tts_numbers",
    "check_tts_markup",
    "check_tts_control_chars",
    "check_tts_acronyms",
    "normalize_tts_text",
    # repetition
    "RULE_REPETITION_LOCAL",
    "RULE_REPETITION_CTA",
    "check_local_repetition",
    "check_cta_frequency",
    # duration
    "RULE_SPEECH_DURATION",
    "check_segment_duration",
    # full script
    "RULE_REPETITION_CROSS",
    "RULE_CLAIM_CONTRADICTION",
    "RULE_COVERAGE_REQUIRED",
    "RULE_CTA_PACING",
    "RULE_TONE_CONSISTENCY",
    "RULE_TRANSITION_ORDER",
    "RULE_SPEECH_DURATION_TOTAL",
    "check_cross_segment_repetition",
    "check_contradictory_claims",
    "check_required_coverage",
    "check_cta_pacing",
    "check_tone_consistency",
    "check_transition_policy",
    "check_total_duration",
]
