"""Generate and Fix prompt builders (tasks 6.3, 6.4; design Decision 5).

Generate is creative: project skill + relevant generation constraints +
authoritative context + requested duration/intent + plan/segment assignment
+ compact continuity state. Fix is constrained repair: immutable source +
exact failed rules' repair instructions + only the authoritative facts
needed to prevent claim drift — and it explicitly forbids broad rewrites,
new claims, and new CTAs unless a failed rule requires them. The two
contracts never share content beyond the typed ``PromptParts`` shell.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel

from ..duration import gate_duration_band
from .context_builder import AuthoritativeContext
from .continuity import ContinuityState
from .intent import ScriptIntent, TransitionContext, transition_guidance

__all__ = [
    "PromptParts",
    "OversizedContextError",
    "PromptBuildError",
    "build_generate_prompt",
    "build_repair_prompt",
    "estimate_tokens",
    "guard_budget",
]

# Token-estimation heuristic: one token ~= 4 characters (chars/4).
_CHARS_PER_TOKEN: int = 4

# Spoken-output budget for the segment length target (15.4 real-LLM E2E fix).
# SpeechDurationEstimator is highly non-linear: currency tokens
# (dong/nghin/trieu), numbers and acronyms carry multiplicative pauses
# (currency 1.6x, acronym 1.5x, number 1.4x) so sales prose with one
# spelled price inflates ~4x. A raw word budget (6.25 w/s) therefore
# overshoots the gate massively (1875 words ~= 9000 chars ~= >1000s for a
# 300s target). Calibration on the real estimator shows ~2.6 chars/s for
# typical sales prose with one price+SKU per ~260 chars (780 chars ~= 302s,
# 1100 chars ~= 427s). The prompt uses this calibrated CHAR budget as the
# primary signal and states the gate band explicitly so the model stays
# inside [0.5*T, 1.5*T].
_CALIBRATED_CHARS_PER_SECOND: float = 2.6
_SPOKEN_WORDS_PER_SECOND: float = 1.0 / 0.160  # kept for reference only
# Plain Vietnamese prose with NO price/SKU/acronym compact tokens reads at the
# estimator's native ~6.25 words/s (~16.5 chars/s at ~2.65 chars/word). A
# segment that carries no compact tokens must fill far more characters than a
# 2.6 chars/s budget allows; the prompt switches to this rate so it does not
# under-fill the duration gate (15.4 real-E2E finding: a no-price segment at
# 2.6 chars/s came out ~46s, far below the [150, 450] band).
_PLAIN_CHARS_PER_SECOND: float = 15.5


def estimate_tokens(text: str) -> int:
    """Rough token estimate for prompt-budget guarding (chars/4 heuristic).

    Documented as an estimate: the model's real tokenizer may differ by a
    constant factor, which the guard's explicit budget covers. Deterministic
    and dependency-free.
    """
    return max(1, len(text) // _CHARS_PER_TOKEN)


class OversizedContextError(ValueError):
    """Raised when assembled prompt parts exceed the configured token budget.

    Deliberately NOT a truncation: silently dropping skill guidance or
    repair instructions would let a model generate outside its contract, so
    an oversized context fails loudly and predictably (task 6.7).
    """

    def __init__(self, actual_tokens: int, max_tokens: int) -> None:
        super().__init__(f"prompt parts exceed token budget: {actual_tokens} > {max_tokens}")
        self.actual_tokens = actual_tokens
        self.max_tokens = max_tokens


class PromptBuildError(ValueError):
    """Raised when a builder receives input it cannot render (task 6.4)."""


class PromptParts(BaseModel):
    """Structured prompt assembly: system / context / user parts.

    Model-facing request is built from these three strings; the parts carry
    no tools, no function-calling schema, and no iteration/control hooks
    (task 6.6). ``parts_keywords`` is a stable, machine-readable marker of
    the contract kind for prompt-contract tests (6.5).
    """

    system: str = ""
    context: str = ""
    user: str = ""
    parts_keywords: tuple[str, ...] = ()


def _quote_section(title: str, text: str) -> str:
    return f"## {title}\n{text.strip()}\n"


def _render_authoritative_context(ctx: AuthoritativeContext) -> str:
    """Render the minimal authoritative context as prompt text.

    ONLY the slices a generation operation may reference are rendered —
    unrelated catalog data never enters a prompt (task 6.1).
    """
    lines: list[str] = []
    if ctx.shop:
        lines.append("Shop: " + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.shop.items())))
    if ctx.persona:
        lines.append("Persona: " + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.persona.items())))
    if ctx.campaign:
        lines.append("Campaign: " + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.campaign.items())))
    if ctx.product:
        lines.append("Product: " + "; ".join(f"{k}: {v}" for k, v in sorted(ctx.product.items())))
    if ctx.promotions:
        lines.append("Promotions:")
        for promo in ctx.promotions:
            lines.append(" - " + "; ".join(f"{k}: {v}" for k, v in sorted(promo.items())))
    if ctx.facts:
        lines.append("Authoritative facts (may be claimed):")
        for fact in ctx.facts:
            lines.append(" - " + "; ".join(f"{k}: {v}" for k, v in sorted(fact.items())))
    return "\n".join(lines)


def _render_continuity(state: ContinuityState) -> str:
    """Render the compact continuity state (task 8.8 bounded context)."""
    lines: list[str] = [
        "Continuity state (bounded):",
    ]
    if state.previous_segment_tail:
        lines.append("Previous-segment tail: " + state.previous_segment_tail)
    if state.covered_fact_ids:
        lines.append("Covered fact IDs: " + ", ".join(sorted(state.covered_fact_ids)))
    if state.handled_objection_ids:
        lines.append("Handled objection IDs: " + ", ".join(sorted(state.handled_objection_ids)))
    lines.append(f"CTA count so far: {state.cta_count}")
    if state.opening_fingerprints:
        lines.append("Used opening fingerprints: " + ", ".join(sorted(state.opening_fingerprints)))
    if state.used_ctas:
        # 15.4 real-LLM E2E: a real LLM reuses the same CTA/closing across
        # NON-adjacent segments (e.g. 1,3,5) because the prompt only showed the
        # previous segment's tail. Listing every already-used CTA lets the
        # model pick a genuinely fresh one, so REPETITION_CROSS stays quiet.
        lines.append("Used CTA/closing phrases: " + ", ".join(sorted(state.used_ctas)))
    if state.closing_fingerprints:
        lines.append("Used closing fingerprints: " + ", ".join(sorted(state.closing_fingerprints)))
    if state.last_topic:
        lines.append(f"Last topic: {state.last_topic}")
    if state.next_topic:
        lines.append(f"Next topic: {state.next_topic}")
    return "\n".join(lines)


def _render_transition(ctx: TransitionContext) -> str:
    """Render transition-policy guidance plus allowed summaries only."""
    lines: list[str] = [transition_guidance(ctx)]
    if ctx.previous_product_summary:
        lines.append(f"Previous product summary: {ctx.previous_product_summary}")
    if ctx.next_product_summary:
        lines.append(f"Next product summary: {ctx.next_product_summary}")
    return "\n".join(lines)


def _segment_assignment_text(plan: Optional[dict], segment_index: Optional[int]) -> str:
    """Render the plan/segment assignment block (used by Generate only)."""
    parts: list[str] = []
    if plan:
        parts.append("## Plan assignment")
        parts.append("; ".join(f"{k}: {v}" for k, v in sorted(plan.items())))
    if segment_index is not None:
        parts.append(
            f"## Segment assignment\n"
            f"You are writing segment {segment_index} of this product script."
        )
    return "\n".join(parts)


def build_generate_prompt(
    skill_text: str,
    generation_constraints: list[str],
    context: AuthoritativeContext,
    duration_s: int,
    intent: ScriptIntent,
    transition: TransitionContext,
    *,
    plan: Optional[dict] = None,
    segment_index: Optional[int] = None,
    continuity: Optional[ContinuityState] = None,
    repair_keywords: tuple[str, ...] = (),
    compact_tokens: bool = True,
    chars_per_second: Optional[float] = None,
    price_in_words: bool = True,
) -> PromptParts:
    """Build the creative Generate prompt (tasks 6.3, 6.5).

    The model input includes the project-owned sales skill guidance, the
    relevant generation constraints, authoritative context, requested
    duration/intent, transition policy, and the plan/segment assignment plus
    compact continuity state. Repair-only instructions are never included;
    the returned parts carry only the ``GENERATE_SCRIPT_SEGMENT`` marker.

    ``compact_tokens`` selects the length budget: a segment that carries
    price/SKU/acronym compact tokens inflates the duration estimate; one
    without them reads at the plain prose rate (~15.5 chars/s) and must fill
    far more characters. ``chars_per_second`` overrides the compact rate for
    the exact compact-token mix of this segment (15.4 calibration: a spelled
    SKU inflates ~2.4x, a single digit price ~2.2x, so per-segment rates are
    calibrated rather than one constant).
    """
    system_blocks: list[str] = [
        skill_text.strip() or "(no sales skill provided)",
        "## Generation constraints",
        *(f"- {c}" for c in generation_constraints if c),
    ]
    context_blocks: list[str] = [
        _quote_section("Authoritative context", _render_authoritative_context(context)),
        _quote_section(
            "Requested duration and intent",
            f"Requested spoken duration: {duration_s} seconds\nIntent: {intent.intent}",
        ),
        _quote_section("Transition policy", _render_transition(transition)),
    ]
    if continuity is not None:
        context_blocks.append(_quote_section("Continuity state", _render_continuity(continuity)))

    assignment = _segment_assignment_text(plan, segment_index)
    user_parts: list[str] = [
        "GENERATE_SCRIPT_SEGMENT",
        "Write the assigned product script segment in natural, spoken, "
        "VieNeu-ready Vietnamese. Follow the skill guidance and constraints "
        "exactly; claim only facts from the authoritative context.",
    ]
    if assignment:
        user_parts.append(assignment)
    if continuity is not None:
        user_parts.append(
            "Do not recap the whole product; bridge only from the continuity state above."
        )
    # Calibrated length + anti-repetition discipline for real-LLM E2E (15.4).
    # The gate estimates duration with multiplicative currency/acronym/number
    # factors, so a raw word budget (6.25 w/s) overshoots massively. Use the
    # calibrated char budget as the primary signal and state the SAME gate
    # band (``gate_duration_band`` 50%-150%, reviewer R9.4) so prompt and gate
    # can never disagree.
    if segment_index is not None and duration_s > 0:
        band_min_s, band_max_s = gate_duration_band(duration_s)
        target_min_s = int(band_min_s)
        # The prompt char budget targets ~1.5T (not 2.0T) so a segment written
        # at the stated ceiling stays inside the gate's 1.5T max.
        target_max_s = int(band_max_s)
        target_words = int(duration_s * _SPOKEN_WORDS_PER_SECOND)
        if compact_tokens:
            rate = chars_per_second or _CALIBRATED_CHARS_PER_SECOND
            target_chars = int(duration_s * rate)
            max_chars = int(duration_s * rate * 1.5)
            length_line = (
                f"Write {target_chars} characters (about {target_words} words would be "
                f"~{duration_s}s in plain prose, but with prices/SKUs the real "
                "budget is far smaller). In practice that is "
                f"{target_chars} characters -- roughly {max(4, target_chars // 110)} "
                "short paragraphs. "
                f"STRICT LIMITS: do not exceed {max_chars} characters; "
                "the gate accepts up to "
                f"{target_max_s}s per segment, so aim for {target_chars} and "
                "never pass max_chars. "
                "The gate estimates duration with currency/number/acronym "
                "multipliers (each price word like dong/nghin/trieu multiplies "
                "duration 1.6x, acronyms 1.5x), so a segment at max_chars is "
                "already at the ceiling and FAILS if it goes further. "
                f"State the price EXACTLY ONCE in THIS segment, in a written "
                f"form DIFFERENT from every prior segment's price form, "
                + (
                    "e.g. spelled out in Vietnamese WORDS (hai trieu chin tram "
                    "chin muoi nghin dong), never in digits (digits under-count "
                    "the spoken duration gate)"
                    if price_in_words
                    else "e.g. as digits (2.990.000d) keeping the d currency "
                    "symbol -- never the bare number, and never in words"
                )
                + (
                    ". You MUST state it -- a segment without the price token "
                    "under-counts spoken duration and fails the duration gate"
                    if not price_in_words
                    else ""
                )
                + ", and never restate it within THIS segment (a repeated price "
                "phrase fails REPETITION_LOCAL). If the SKU is listed, state it "
                "once as spaced Vietnamese words (e.g. N F hai khong hai sau), "
                "never as NF or NF-2026 (triggers TTS_ACRONYM). Do not repeat "
                "the opening greeting or hook anywhere in the segment."
            )
        else:
            # No compact tokens in this segment: the estimator reads it at the
            # plain prose rate (~15.5 chars/s), so a small char budget under-fills
            # the duration gate. Budget in words (the estimator's native unit).
            target_chars = int(duration_s * _PLAIN_CHARS_PER_SECOND)
            max_chars = int(duration_s * 22.0)
            length_line = (
                f"Write {target_chars} characters (~{target_words} spoken words, "
                f"~{duration_s} seconds of plain prose). The gate accepts "
                f"[{target_min_s}s, {target_max_s}s] spoken; aim for the middle "
                f"(~{duration_s}s). In practice that is about "
                f"{target_words} words -- roughly {max(4, target_words // 40)} "
                "short paragraphs. "
                f"STRICT LIMITS: do not exceed {max_chars} characters; a "
                "character-count that low would be under the minimum spoken "
                "duration and FAIL. Vary vocabulary and sentence structure; "
                "do not repeat any 3-word phrase more than once."
            )
        system_blocks.append(
            "## Segment length target (CALIBRATED - obey exactly)\n"
            f"For this segment the gate accepts [{target_min_s}s, {target_max_s}s] "
            f"spoken; aim for the middle (~{duration_s}s). HARD MINIMUM: at least "
            f"{target_min_s}s spoken -- a shorter segment FAILS. If in doubt, write "
            f"MORE, never less. {length_line}"
        )
        system_blocks.append(
            "## Uniqueness and claim discipline (gate-critical)\n"
            "Open this segment with a hook DIFFERENT from every other segment; "
            "never reuse a sentence, phrase, or opening pattern from another "
            "segment of the same script.\n"
            "CROSS-SEGMENT REPETITION is a script-level ERROR: a 4-word phrase "
            "appearing in 3+ segments fails REPETITION_CROSS (recurring in only "
            "2 segments is allowed for a same-product script). The previous "
            "segment(s) tail and used opening fingerprints are in the Continuity "
            "state above -- read them and avoid every 4-gram they contain. After "
            "the first segment has named the product (May loc nuoc NanoFresh), "
            "later segments MUST use pronouns (san pham, thiet bi, em nay) and "
            "MUST NOT restate the full product name -- paraphrase or omit it. The "
            "PRICE is stated EXACTLY ONCE PER SEGMENT, always in a DIFFERENT "
            "written form than every prior segment (e.g. digits '2.990.000d' in "
            "one, full words 'hai trieu chin tram chin muoi nghin dong' in "
            "another, rounded 'khoang ba trieu dong' in a third) so the exact "
            "price 4-gram never repeats across segments (REPETITION_CROSS) and "
            "every segment still carries the compact price token that fills its "
            "spoken-duration budget. Vary sentence structure completely; do not "
            "reuse the same benefit sentence across segments. Each segment must "
            "cover DIFFERENT allowed claims from the authoritative context -- do "
            "not repeat a claim sentence that another segment already used.\n"
            "State your segment's allowed claim EXACTLY ONCE -- never restate it; "
            "a repeated claim phrase fails REPETITION_LOCAL. Count every 3-word "
            "phrase: 3 uses of one 3-gram (e.g. 'quy vi chi can') fails "
            "REPETITION_LOCAL -- vary with synonyms (ban chi viec, chi can, quy "
            "khach chi). Say the greeting "
            "phrase (theo doi buoi phat truc tiep / phien phat song) exactly "
            "ONCE at the opening -- do NOT repeat it at the close; close with a "
            "CTA or thanks instead. Use a CTA DIFFERENT from EVERY "
            "CTA already used in earlier segments -- the Continuity state lists "
            "'Used CTA/closing phrases' and 'Used closing fingerprints'; you "
            "MUST NOT reuse any of them, and your closing's last 4 words must "
            "differ from every prior closing's last 4 words (e.g. if 'bam nut "
            "mua ngay ben duoi' was used, close with a different CTA such as "
            "'dat hang ngay hom nay') -- a CTA repeated across segments fails "
            "REPETITION_CROSS. "
            "If the continuity tail already contains the opening greeting "
            "phrase, do NOT open with it again. The FIRST 4 words of your "
            "opening must differ from every other segment's first 4 words: if "
            "the previous segment opened with 'chung ta cung tim hieu', open "
            "this one with a different phrase (e.g. 'hay lang nghe', 'mot "
            "diem dang chu y', 'ben canh do', 'dieu dau tien') -- a repeated "
            "opening 4-gram fails REPETITION_CROSS.\n"
            "Claim ONLY facts listed in the authoritative context below -- do not "
            "invent benefits, materials, health effects, or comparisons. For each "
            "factual sentence, embed the allowed claim's key phrase VERBATIM "
            '(the gate matches the exact phrase): e.g. use "bộ lọc loại bỏ tạp '
            'chất", "lõi lọc thay sau mười hai tháng", "thiết kế gọn nhẹ cho '
            'gia đình", "không dùng điện trong quá trình lọc" as-is inside '
            "your sentence. Do not paraphrase a claim into new wording, and do "
            "not add new claim verbs (lam/chua/an toan/hieu qua/tang/giam etc.) "
            "beyond what the allowed claims support. A factual sentence that "
            "does not contain an allowed phrase verbatim fails CLAIM_FACTUAL. "
            'GOOD claim sentence: "Bộ lọc loại bỏ tạp chất giúp nguồn nước '
            'trong lành hơn mỗi ngày." BAD: "Nhiều gia đình mong muốn một '
            'thiết bị làm sạch hoàn hảo." (asserts a new "làm" claim with no '
            "allowed phrase). "
            "In NON-claim sentences (openers, transitions, CTA), avoid the "
            "claim-trigger words lam/chua/an toan/hieu qua/tang/giam entirely -- "
            'rephrase so no claim verb appears outside a real claim. ("có" and '
            '"giúp" are NOT claim triggers and may be used freely.) '
            'Write every Vietnamese word with its correct tone marks: "dùng" '
            '(to use) keeps the huyền mark, "dừng" (to stop) keeps the nặng '
            'mark on ư -- never write the toneless "dung". The gate also flags '
            '"dung" inside compound words, so AVOID words like "nội dung", '
            '"dung lượng", "dung tích" entirely: say "thông tin", "phần '
            'tiếp theo", "dung tích" -> "thể tích". The gate also flags the '
            'bare "di" (as in "di chuyển") as a gi/d confusion: write '
            '"đi lại" or "di chuyển" -> "thay đổi vị trí". (Any "dung" '
            'or bare "di" token fails VN_SPELLING_GI_D.)'
        )
    return PromptParts(
        system="\n\n".join(block for block in system_blocks if block),
        context="\n\n".join(block for block in context_blocks if block),
        user="\n\n".join(user_parts),
        parts_keywords=("GENERATE_SCRIPT_SEGMENT",),
    )


def build_repair_prompt(
    source_text: str,
    failed_rule_ids: list[str],
    rule_repair_instructions: list[str],
    authoritative_facts: AuthoritativeContext,
    *,
    segment_index: Optional[int] = None,
    target_duration_s: Optional[float] = None,
) -> PromptParts:
    """Build the constrained Fix prompt (tasks 6.4, 6.5).

    Inputs are the immutable source text, the exact failed rule IDs, and the
    repair instructions for exactly those rules, plus ONLY the authoritative
    facts needed to prevent claim drift. The prompt explicitly forbids broad
    rewrites, new claims, and new CTAs unless a failed rule requires them.
    The repair prompt never includes the sales guidance and never includes
    repair-unrelated rules.

    ``segment_index`` scopes the repair to ONE segment (reviewer R9.2/3.3:
    bounded in-place segment repair during Generate); ``None`` means a
    full-script Fix (no index rendered).
    """
    if not source_text.strip():
        raise PromptBuildError("repair source text is empty")
    if not failed_rule_ids:
        raise PromptBuildError("repair requires at least one failed rule id")
    if len(failed_rule_ids) != len(rule_repair_instructions):
        raise PromptBuildError("repair instructions must match failed rule ids one-to-one")

    system_blocks: list[str] = [
        "You are repairing a gate-failed script version with MINIMAL changes.",
        "## Constraints",
        "Fix ONLY the failed rules listed below. Do NOT rewrite the script, "
        "do NOT add new claims, and do NOT add new calls to action unless a "
        "listed failed rule requires it.",
        "Preserve compliant wording, meaning, structure, tone, and factual "
        "claims unless a listed failed rule requires a change.",
        "Claim only facts from the authoritative context below.",
    ]
    context_blocks: list[str] = [
        _quote_section(
            "Failed rules",
            "\n".join(
                f"- {rule_id}: {instruction}"
                for rule_id, instruction in zip(failed_rule_ids, rule_repair_instructions)
                if instruction
            ),
        ),
        _quote_section(
            "Authoritative facts (anti-drift)",
            _render_authoritative_context(authoritative_facts),
        ),
    ]
    user_parts: list[str] = [
        "REPAIR_SCRIPT_SEGMENT",
        "Apply the minimum edits to the source text below that satisfy the failed rules.",
        f"## Immutable source text\n{source_text.strip()}",
        "Return the repaired full text.",
    ]
    if segment_index is not None:
        user_parts.append(f"You are repairing segment {segment_index} of this product script.")
    if target_duration_s is not None:
        # Reviewer R9.6: a duration repair must know how much content to write —
        # the 15.4 repair under-produced (too-short) or over-trimmed (too-long)
        # because it never saw the target. Direction-neutral: the failed rule's
        # own message says which direction, this line gives the magnitude.
        user_parts.append(
            f"Target spoken duration for this segment: ~{target_duration_s:.0f} seconds. "
            "Adjust the content length to land in the accepted band: add NEW "
            "distinct content if too short, trim redundant filler if too long. "
            "Always KEEP the compact price/number tokens (they carry the "
            "spoken-duration estimate)."
        )
    return PromptParts(
        system="\n\n".join(system_blocks),
        context="\n\n".join(context_blocks),
        user="\n\n".join(user_parts),
        parts_keywords=("REPAIR_SCRIPT_SEGMENT", *failed_rule_ids),
    )


def guard_budget(parts: PromptParts, max_tokens: int) -> PromptParts:
    """Guard the assembled parts against the token budget (task 6.7).

    Raises:
        OversizedContextError: total estimated tokens exceed ``max_tokens``.
            Never truncates — critical constraints must not be silently
            dropped.
    """
    total = (
        estimate_tokens(parts.system) + estimate_tokens(parts.context) + estimate_tokens(parts.user)
    )
    if total > max_tokens:
        raise OversizedContextError(total, max_tokens)
    return parts
