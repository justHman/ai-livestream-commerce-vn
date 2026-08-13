"""Shared P010-script/P020-Q&A fixtures for the speech arbiter (tasks 14.11, 14.12).

One deterministic P010 product script (8 sentences, mixed terminators), one
high-priority P020 fast-charging cluster, and the system-boundary fakes the
scenario tests drive: a recording speech service with injectable failures and
an event gate for mid-sentence proofs, plus a recording Q&A resolver.

The fixtures depend on the REAL C13 modules that have landed:

- ``backend.application.live_runtime.sentence_speaker`` (13.4-13.7): the
  real ``SentenceSpeechService`` protocol, ``ScriptSentencePlayer`` and
  ``SentenceCompletionError`` — the sentence player is used for REAL in these
  fixtures, so cursor-advancement semantics are the production ones.
- ``backend.application.live_runtime.sentence_map``/``script_cursor.py``
  (13.1-13.3): ``build_p010_script_map`` / ``build_script_cursor`` build the
  real map/cursor over the exact P010 artifact.
- ``backend.application.live_runtime.pending_qa`` / ``qa_resolver.py``
  (14.4-14.6): the real ``PendingQaStore``/``QaHysteresisConfig``/``QaResolution``
  contracts drive the boundary and failure scenarios.

``P010_SENTENCES`` is authored so the real deterministic derivation yields
EXACTLY the same 8 spans (mixed terminators ``. ! ? …``, single U+2026
ellipsis — a ``...`` run would split into separate spans). The speech arbiter
itself (``speech_arbiter.py``, task 14.1) is a parallel implementer's module;
these fixtures stay decoupled from its internals.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from backend.application.agentic_director.fast_path import ClusterEnvelope
from backend.application.live_runtime.pending_qa import PendingQaStore
from backend.application.live_runtime.qa_resolver import QaResolution
from backend.application.live_runtime.sentence_speaker import SentenceCompletionError

__all__ = [
    "P010_SCRIPT",
    "P010_SENTENCES",
    "P020_QA_ENVELOPE",
    "P020_ANSWER_TEXT",
    "QNA_LEAD_IN",
    "RESUME_BRIDGE_P010",
    "FakeP010Cursor",
    "RecordingQaResolver",
    "RecordingSpeechFake",
    "build_p010_script_map",
    "build_p020_answer",
    "build_pending_qa_store",
    "build_script_cursor",
]

# --- P010 approved script (8 sentences, mixed terminators . ! ? ...) ---------
# Index 6 (sentence 7) is the mid-script interruption anchor; index 7
# (sentence 8) is the exact resume target after P020 Q&A.
P010_SENTENCES: tuple[str, ...] = (
    "Chào các anh chị, hôm nay em giới thiệu sản phẩm P010 nha.",
    "P010 là loa bluetooth mini có thiết kế nhỏ gọn, dễ mang theo.",
    "Loa có thời lượng pin lên tới 12 tiếng liên tục!",
    "P010 có giá 1.2 triệu đồng, đang giảm còn 990 nghìn.",
    "Các anh chị nghe thử chất âm xem sao nhé?",
    "Chất âm của P010 trong trẻo, bass chắc, nghe rất đã…",
    "Em lưu ý là P010 không hỗ trợ sạc nhanh, anh chị nhé!",
    "Còn bây giờ em mời anh chị đặt ngay để được ưu đãi nha.",
)
P010_SCRIPT: str = "".join(P010_SENTENCES)


# --- P020 fast-charging cluster (high priority, beats any P010 candidate) ----
# Duck-typed ``ClusterEnvelope`` (the C12 ``FakeEnvelope`` pattern): the
# Protocol is structural, so an ordinary class instance satisfies it.
class _P020Envelope:
    """High-priority P020 fast-charging cluster (Decision 9 envelope shape)."""

    cluster_id = "cl-p020-fastcharge"
    intent = "sạc nhanh"
    message_count = 18
    unique_viewer_count = 12
    representative_questions = ("P020 có sạc nhanh không?",)
    product_candidates = (("P020", 0.97),)
    resolved_product_ids = ("P020",)
    ranking_score = 0.95
    novelty = 0.6
    current_script_product_id = "P020"
    source_platform_counts = (("tiktok", 15), ("shopee", 3))


P020_QA_ENVELOPE: ClusterEnvelope = _P020Envelope()  # type: ignore[type-abstract]

# Deterministic natural lead-in + grounded answer (design Decision 19): the
# representative question is paraphrased, never read verbatim as a standalone.
QNA_LEAD_IN = "Em thấy nhiều anh chị đang hỏi P020 có hỗ trợ sạc nhanh không. "
P020_ANSWER_TEXT = "P020 có sạc nhanh 65W nha."
# Deterministic resume bridge back to the script product (Decision 19).
# Ends with "." matching the canonical DEFAULT_RESUME_BRIDGE_TEMPLATE.
RESUME_BRIDGE_P010 = "Rồi, em tiếp tục với P010 nhé."


def build_p020_answer() -> str:
    """The single combined Q&A speech text: lead-in + grounded answer."""
    return QNA_LEAD_IN + P020_ANSWER_TEXT


# --- real C13 map/cursor builders (lazy imports keep fixture import light) ---


def build_p010_script_map():
    """Real ``derive_sentence_map`` output for P010 (task 13.1-13.2).

    Returns the real map derived from the exact ``P010_SCRIPT``; the
    concatenation check guards against the derivation drifting from the
    authored 8-sentence contract.
    """
    from backend.application.live_runtime.sentence_map import derive_sentence_map

    sentence_map = derive_sentence_map(P010_SCRIPT)
    span_texts = tuple(span.text for span in sentence_map.spans)
    if "".join(span_texts) != P010_SCRIPT:
        raise RuntimeError(
            f"sentence_map derivation corrupted the approved artifact: "
            f"expected {P010_SCRIPT!r}, got {''.join(span_texts)!r}"
        )
    return sentence_map


# --- system-boundary fakes ---------------------------------------------------


class FakeP010Cursor:
    """Duck-typed ``CursorLike`` over the exact P010 sentences (test double).

    Mirrors the real ``ScriptCursor`` surface the player/arbiter drive
    (``current_sentence``/``complete_current``/``finished``/``position``/
    ``checkpoint_next_before_qa``) so the 14.11/14.12 scenarios run TODAY
    against the real ``ScriptSentencePlayer``; the real cursor
    (``script_cursor.py``, parallel task 13.3) swaps in once it lands.
    """

    def __init__(self, sentences: tuple[str, ...] = P010_SENTENCES) -> None:
        self.sentences = sentences
        self._index = 0
        self.completed: list[str] = []

    @property
    def finished(self) -> bool:
        return self._index >= len(self.sentences)

    @property
    def sentence_index(self) -> int:
        return self._index

    @property
    def last_completed_sentence_index(self) -> int | None:
        return self._index - 1 if self._index > 0 else None

    def current_sentence(self):
        span = SentenceSpan(index=self._index, text=self.sentences[self._index])
        return span

    def complete_current(self) -> None:
        self.completed.append(self.sentences[self._index])
        self._index += 1

    def position(self):
        return _P010Position(product_id="P010")

    def checkpoint_next_before_qa(self) -> None:
        pass


@dataclass(frozen=True, slots=True)
class _P010Position:
    """Minimal position view for the resume bridge (product id only)."""

    product_id: str


@dataclass(frozen=True, slots=True)
class SentenceSpan:
    """Exact sentence span (mirrors ``cursor_typing.SentenceSpan``)."""

    index: int
    text: str


class RecordingSpeechFake:
    """``SentenceSpeechService`` fake: records every (session_id, text) spoken.

    Failure injection:

    - ``fail_next`` / ``fail_after(n)`` raise ``SentenceCompletionError`` on
      the Nth call (before any recording) — used for the 14.12 variants.
    - ``slow_until(call_number)`` suspends call N on ``started_events[N]``
      until ``release(call_number)`` — the mid-sentence gate for the 14.11
      non-preemption proof (an awaitable gate; no real orchestrator needed).
    """

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str]] = []
        self._fail_call: int | None = None
        self._call_count = 0
        self._slow_calls: dict[int, asyncio.Event] = {}
        self.started_events: list[asyncio.Event] = []

    def fail_next(self) -> None:
        self._fail_call = self._call_count + 1

    def fail_after(self, n: int) -> None:
        """Fail the Nth future call (n >= 1), like ``fail_next`` after n-1 passes."""
        if n < 1:
            raise ValueError("fail_after(n) requires n >= 1")
        self._fail_call = self._call_count + n

    def slow_until(self, call_number: int) -> asyncio.Event:
        """Return the gate event for call ``call_number`` (1-based)."""
        gate = asyncio.Event()
        self._slow_calls[call_number] = gate
        self.started_events.append(gate)
        return gate

    async def release(self, call_number: int) -> None:
        self._slow_calls[call_number].set()

    async def speak_sentence(self, session_id: str, text: str) -> str:
        self._call_count += 1
        call_number = self._call_count
        if call_number == self._fail_call:
            raise SentenceCompletionError(f"injected failure on call {call_number}")
        if call_number in self._slow_calls:
            await self._slow_calls[call_number].wait()
        self.spoken.append((session_id, text))
        return text


class RecordingQaResolver:
    """``QaResolutionService`` fake: records every resolve_qa call.

    Satisfies the real protocol from ``qa_resolver.py`` (``resolve_qa`` +
    ``prefetch_stable_evidence``). Outcome is configurable per call index:

    - answer: returns the deterministic lead-in + grounded answer text;
    - unavailable: returns the real ``QaResolution.unavailable`` (the runtime
      then skips Q&A and resumes the script — policy asserted by the 14.12
      fixture);
    - ``raise_on_next``: raises before returning anything (the arbiter's real
      failure policy decides the outcome — the fixture asserts it, see
      ``test_qa_failure_fixture.py``).
    """

    def __init__(self, outcome: str = "answer", reason: str = "") -> None:
        self.outcome = outcome
        self.reason = reason
        self.calls: list[str] = []
        self.prefetches: list[str] = []
        self._raise_calls: set[int] = set()
        self._call_count = 0

    def raise_on_next(self) -> None:
        self._raise_calls.add(self._call_count + 1)

    def prefetch_stable_evidence(self, candidate) -> None:
        self.prefetches.append(candidate.cluster_id)

    async def resolve_qa(self, candidate) -> QaResolution:
        self._call_count += 1
        self.calls.append(candidate.cluster_id)
        if self._call_count in self._raise_calls:
            raise RuntimeError("injected Q&A resolver failure")
        if self.outcome == "unavailable":
            return QaResolution.unavailable(reason=self.reason)
        return QaResolution.answer(speech_text=build_p020_answer())


def build_script_cursor(sentence_map=None):
    """Real ``ScriptCursor`` over the P010 sentence map (tasks 13.3, 13.8).

    The cursor starts at sentence 0; ``complete_current()`` advances to the
    exact next sentence (and marks the script finished at the final span).
    """
    from backend.application.live_runtime.script_cursor import ScriptCursor

    sentence_map = sentence_map if sentence_map is not None else build_p010_script_map()
    return ScriptCursor(sentence_map)


def build_pending_qa_store(
    envelope: ClusterEnvelope = P020_QA_ENVELOPE,
    *,
    now=None,
):
    """``PendingQaStore`` pre-seeded with the P020 fast-charge cluster (14.4).

    ``now`` is an optional fake clock so cooldown/relevance-window behavior
    stays deterministic in the 14.12 failure scenarios.
    """
    store = PendingQaStore(now=now)
    store.update(envelope, now=0.0 if now is None else now())
    return store
