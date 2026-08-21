"""Shared helpers for Change B B6 integration tests (real PG + real ScriptGate).

The real ``ScriptGate`` enforces Vietnamese spelling, profanity, claim,
repetition and SPEECH_DURATION rules. A naive echo LLM fails it, so these
helpers build a deterministic gate-compliant word stream:

- every token is a distinct lowercase ASCII ``CVC`` syllable filtered against
  the profanity lexicon and the ``gi``/``d`` confusion set, so no 3-gram or
  4-gram ever repeats (local or cross-segment repetition cannot fire);
- tokens contain no digits / markup / em-dashes / claim verbs, so the
  commerce-claim, TTS and format rules stay silent;
- ``_SEGMENT_WORDS`` words per segment (~171s) keeps every segment inside the
  segment [10, 180]s bounds while two segments sum past the full-script
  300s lower bound.

``FakeLlm`` is a duck-typed ``EngineManager.get_llm_fn`` result: it inspects
the prompt marker to return a parseable plan for planning calls and a
gate-compliant segment chunk for segment calls.
"""

from __future__ import annotations

import re
import time

from backend.application.script_authoring.gate.rules.profanity import load_curated_lexicon
from backend.application.script_authoring.gate.rules.vietnamese import _GI_D_WORDS

# Words per segment: ~171s of spoken text (within [10, 180]s segment bounds).
_SEGMENT_WORDS = 280
# Bank capacity (20 consonants x 5 vowels x 20 consonants, filtered): ~1800.
_CONSONANTS = "bcdfghjklmnpqrstvwz"
_VOWELS = "aeiou"
_GI_D_BANNED = set(_GI_D_WORDS.keys())


def _build_word_bank() -> list[str]:
    lexicon = load_curated_lexicon()
    bank: list[str] = []
    for c in _CONSONANTS:
        for v in _VOWELS:
            for c2 in _CONSONANTS:
                word = c + v + c2
                if lexicon.is_offensive(word):
                    continue
                if word.lower() in _GI_D_BANNED:
                    continue
                bank.append(word)
    return bank


WORD_BANK: list[str] = _build_word_bank()


def gate_compliant_text(start: int, count: int = _SEGMENT_WORDS) -> str:
    """Return ``count`` distinct gate-compliant tokens from the bank."""
    chunk = WORD_BANK[start : start + count]
    parts: list[str] = []
    for i, word in enumerate(chunk):
        parts.append(word)
        if (i + 1) % 12 == 0 and i != len(chunk) - 1:
            parts[-1] = word + "."
    return " ".join(parts) + ("." if parts else "")


PLAN_RESPONSE = "1. Mở đầu|Giới thiệu sản phẩm|600\n2. Nội dung|Lợi ích chính|600\n"


class FakeLlm:
    """Controllable sync ``(text) -> str`` LLM for integration tests.

    Planning prompts (marker ``PLAN_THE_SCRIPT_SEGMENTS``) return
    ``PLAN_RESPONSE``; segment prompts return either a per-index override, a
    bank chunk, or a supplied default. ``delay`` slows segment calls so a batch
    can be cancelled while still running.
    """

    def __init__(
        self,
        *,
        plan_text: str = PLAN_RESPONSE,
        segment_by_index: dict[int, str] | None = None,
        default_segment: str | None = None,
        delay: float = 0.0,
    ) -> None:
        self.plan_text = plan_text
        self.segment_by_index = segment_by_index or {}
        self.default_segment = default_segment
        self.delay = delay
        self.segment_calls = 0

    @staticmethod
    def _segment_index(prompt: str) -> int | None:
        match = re.search(r"segment\s+(\d+)", prompt, re.IGNORECASE)
        return int(match.group(1)) if match else None

    def __call__(self, prompt: str) -> str:
        if self.delay:
            time.sleep(self.delay)
        if "PLAN_THE_SCRIPT_SEGMENTS" in prompt:
            return self.plan_text
        index = self._segment_index(prompt)
        self.segment_calls += 1
        if index is not None and index in self.segment_by_index:
            return self.segment_by_index[index]
        if self.default_segment is not None:
            return self.default_segment
        offset = (self.segment_calls - 1) * _SEGMENT_WORDS
        return gate_compliant_text(offset % len(WORD_BANK))


class FakeEngineManager:
    """Duck-typed ``EngineManager`` exposing a controllable sync LLM fn."""

    def __init__(self, llm_fn) -> None:
        self._llm_fn = llm_fn
        self._llm_cfg = {"engine": "echo", "model": "fake"}
        self.llm = object()

    @property
    def llm_cfg(self) -> dict:
        return self._llm_cfg

    @property
    def llm_failed(self) -> bool:
        return False

    def get_llm_fn(self):
        return self._llm_fn


def short_segment_text() -> str:
    """A segment that passes the segment gate but is too short for the full
    script (used to drive generation into GATE_FAILED with a plan present).

    Uses a high bank offset so it shares no 4-grams with the long segments
    (which occupy the low offsets), keeping REPETITION_CROSS silent.
    """
    return gate_compliant_text(1500, 40)  # ~24s — too short to reach 300s total with K=2
