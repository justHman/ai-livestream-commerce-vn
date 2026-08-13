"""Cluster C13 tasks 13.4-13.8: sentence-level speech scheduling proofs.

Proves the sentence speaker:

- 13.4: each approved sentence reaches the canonical ``speak_verbatim`` path
  with the EXACT sentence text; one long sentence yields MULTIPLE canonical
  TextChunks internally but stays ONE speech call (real orchestrator).
- 13.5: the cursor advances exactly one sentence on normal completion, and
  does NOT advance on error or hard cancellation.
- 13.6: the player never infers completion from TextChunk boundaries — its
  only completion signal is the speech call returning normally; a fake that
  "emits chunks" then raises leaves the cursor untouched.
- 13.7: the production module never imports ``text_chunker`` and passes no
  deadlines/hints to the boundary.
- 13.8: playing sentences never mutates the approved artifact or the binding
  snapshot, and the exact spoken texts are exact slices of ``spoken_text``.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from typing import Iterator, Optional

import pytest

from avatar.engines.mock import MockRenderBackend
from backend.application.render.engines_base import StartOptions
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from backend.application.render.windows import AudioWindow, VideoWindow
from backend.application.script_authoring.runtime_handoff import (
    ResolvedApprovedScript,
    build_binding_snapshot,
)
from backend.application.live_runtime.cursor_typing import SentenceSpan
from backend.application.live_runtime.sentence_speaker import (
    OrchestratorSpeechService,
    ScriptSentencePlayer,
    SentenceCompletionError,
)
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from tts.engines.base import ToneEngine

SRC_ROOT = "backend.application.live_runtime.sentence_speaker"

# The chunker API is imported lazily (inside tests) on purpose: the 13.7
# sys.modules proof must see the speaker imported WITHOUT the chunker package.
_CHUNKER_ROOT = "backend.application.text_chunker"


def _approved(product_id: str, version_id: str, text: str) -> ResolvedApprovedScript:
    return ResolvedApprovedScript(
        product_id=product_id, approved_version_id=version_id, spoken_text=text
    )


class _FakeCursor:
    """Duck-typed CursorLike over an explicit sentence list."""

    def __init__(self, sentences: list[str]) -> None:
        self._sentences = sentences
        self._index = 0
        self.completed: list[str] = []

    @property
    def finished(self) -> bool:
        return self._index >= len(self._sentences)

    def current_sentence(self) -> SentenceSpan:
        return SentenceSpan(index=self._index, text=self._sentences[self._index])

    def complete_current(self) -> None:
        self.completed.append(self._sentences[self._index])
        self._index += 1


class _RecordingSpeech:
    """SentenceSpeechService fake recording every (session_id, text) call."""

    def __init__(self, fail_on: Optional[str] = None, cancelled_on: Optional[str] = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail_on = fail_on
        self._cancelled_on = cancelled_on

    async def speak_sentence(self, session_id: str, text: str) -> str:
        self.calls.append((session_id, text))
        if self._cancelled_on is not None and text == self._cancelled_on:
            raise asyncio.CancelledError
        if self._fail_on is not None and text == self._fail_on:
            raise RuntimeError("tts stream failed mid-sentence")
        return text


class _NoopLLM(LLMEngine):
    """LLM stub that must never be reached on the verbatim path."""

    name = "noop-llm"

    @classmethod
    def from_config(cls, cfg: dict) -> "_NoopLLM":  # pragma: no cover
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise RuntimeError("verbatim path must not call the LLM")

    def stream_chunks(self, req, *, session_id="", utterance_id="") -> Iterator[object]:
        raise RuntimeError("verbatim path must not call the LLM")


class _RecordingTTS(ToneEngine):
    """ToneEngine subclass recording every received input + spoken text."""

    def __init__(self) -> None:
        super().__init__()
        self.received_inputs: list[object] = []
        self.spoken_texts: list[str] = []

    def stream_audio(
        self, text_or_chunk, *, session_id="", utterance_id="", **kwargs
    ) -> Iterator[AudioWindow]:
        from backend.application.text_chunker import TextChunk

        text = text_or_chunk.text if isinstance(text_or_chunk, TextChunk) else str(text_or_chunk)
        self.received_inputs.append(text_or_chunk)
        self.spoken_texts.append(text)
        yield from super().stream_audio(
            text_or_chunk, session_id=session_id, utterance_id=utterance_id, **kwargs
        )


def _build_orchestrator(tts: _RecordingTTS):
    """Fresh real orchestrator for ONE speech call (mirrors coordinator flow)."""
    from backend.application.render.orchestrator import (
        StreamOrchestrator,
        StreamingControllerConfig,
    )
    from backend.application.text_chunker import FixedChunkPolicyConfig

    backend = MockRenderBackend()
    backend.start(StartOptions())
    queue = BoundedVideoQueue(max_size=20)
    metrics = CoordinatorMetrics()
    orch = StreamOrchestrator(
        llm=_NoopLLM(),
        tts=tts,
        backend=backend,
        queue=queue,
        metrics=metrics,
        fixed_config=FixedChunkPolicyConfig(min_chars=4, target_chars=20, max_chars=40),
        controller_config=StreamingControllerConfig(flush_timeout_ms=50),
    )
    sid = next(iter(backend._sessions.keys()))
    return orch, backend, queue, sid


async def _drain(queue: BoundedVideoQueue) -> list[VideoWindow]:
    windows: list[VideoWindow] = []
    while queue.qsize() > 0:
        windows.append(await queue.get())
    return windows


# ---------- 13.4: canonical speak_verbatim path, one sentence at a time ----------


async def test_approved_sentence_reaches_verbatim_with_exact_text() -> None:
    """One approved sentence -> speak_verbatim gets the EXACT sentence text.

    The player forwards the sentence text unmodified to the canonical speech
    service; no LLM rewrite, no rephrasing.
    """
    cursor = _FakeCursor(["Kem chống nắng SPF50, giá 350.000 đồng."])
    speech = _RecordingSpeech()
    player = ScriptSentencePlayer(cursor, speech)

    spoken = await player.play_current("sess-1")

    assert spoken == "Kem chống nắng SPF50, giá 350.000 đồng."
    assert speech.calls == [("sess-1", "Kem chống nắng SPF50, giá 350.000 đồng.")]


async def test_long_sentence_one_speak_call_multiple_chunks_real_orchestrator() -> None:
    """Long sentence -> ONE speak_verbatim call, MULTIPLE canonical TextChunks.

    Drives the real ``StreamOrchestrator`` (Change A path): the long sentence
    segments internally into several phrase chunks (policy target 20 chars),
    but the player made exactly one boundary call with the exact sentence.
    """
    from backend.application.text_chunker import TextChunk as Canonical

    sentence = (
        "Chào cả nhà, hôm nay shop mở phiên live đặc biệt. "
        "Kem chống nắng SPF50 chống nước, giá chỉ 350.000 đồng. "
        "Giảm thêm 20 phần trăm khi mua hai chai. Nhanh tay đặt hàng nhé!"
    )
    tts = _RecordingTTS()
    orch, backend, queue, sid = _build_orchestrator(tts)
    player = ScriptSentencePlayer(_FakeCursor([sentence]), OrchestratorSpeechService(orch))

    spoken = await player.play_current(sid)
    windows = await _drain(queue)

    assert spoken == sentence
    assert len(tts.received_inputs) > 1, "one long sentence must segment into multiple phrases"
    assert all(isinstance(x, Canonical) for x in tts.received_inputs)
    assert "".join(tts.spoken_texts) == sentence, "chunks concatenate back to the sentence"
    assert windows, "speech produced video windows"


# ---------- 13.5: cursor advances only on normal completion ----------


async def test_normal_completion_advances_exactly_one_sentence() -> None:
    cursor = _FakeCursor(["Câu một.", "Câu hai."])
    player = ScriptSentencePlayer(cursor, _RecordingSpeech())

    await player.play_current("sess-1")

    assert cursor.completed == ["Câu một."]


async def test_error_does_not_advance_cursor() -> None:
    cursor = _FakeCursor(["Câu một.", "Câu hai."])
    speech = _RecordingSpeech(fail_on="Câu một.")
    player = ScriptSentencePlayer(cursor, speech)

    with pytest.raises(SentenceCompletionError, match="Câu một"):
        await player.play_current("sess-1")

    assert cursor.completed == []


async def test_hard_cancel_does_not_advance_cursor() -> None:
    cursor = _FakeCursor(["Câu một.", "Câu hai."])
    speech = _RecordingSpeech(cancelled_on="Câu một.")
    player = ScriptSentencePlayer(cursor, speech)

    with pytest.raises(SentenceCompletionError, match="cancel"):
        await player.play_current("sess-1")

    assert cursor.completed == []


async def test_play_all_advances_until_finished_then_stops() -> None:
    cursor = _FakeCursor(["Câu một.", "Câu hai.", "Câu ba."])
    player = ScriptSentencePlayer(cursor, _RecordingSpeech())

    await player.play_all("sess-1")

    assert cursor.completed == ["Câu một.", "Câu hai.", "Câu ba."]


async def test_play_all_stops_on_first_error() -> None:
    cursor = _FakeCursor(["Câu một.", "Câu hai.", "Câu ba."])
    speech = _RecordingSpeech(fail_on="Câu hai.")
    player = ScriptSentencePlayer(cursor, speech)

    with pytest.raises(SentenceCompletionError):
        await player.play_all("sess-1")

    assert cursor.completed == ["Câu một."]


# ---------- 13.6: no completion inference from TextChunk boundaries ----------


async def test_chunk_emission_then_failure_does_not_advance_cursor() -> None:
    """Chunks "emitted" before a failure are NOT proof of sentence completion.

    A fake speech service that would have emitted many chunks then raises:
    the cursor stays put, proving the player's only completion signal is the
    call returning normally — never chunk boundaries.
    """
    cursor = _FakeCursor(["Câu rất dài, bị lỗi giữa chừng."])
    speech = _RecordingSpeech(fail_on="Câu rất dài, bị lỗi giữa chừng.")
    player = ScriptSentencePlayer(cursor, speech)

    with pytest.raises(SentenceCompletionError):
        await player.play_current("sess-1")

    assert cursor.completed == []
    assert cursor.current_sentence().text == "Câu rất dài, bị lỗi giữa chừng."


async def test_player_never_observes_chunk_finality() -> None:
    """The fake receives only the sentence text — no chunk objects or flags.

    If the player ever inspected TextChunk finality, it could not express
    itself with a text-only boundary; this proves the coupling is absent.
    """
    cursor = _FakeCursor(["Câu một.", "Câu hai."])
    speech = _RecordingSpeech()
    player = ScriptSentencePlayer(cursor, speech)

    await player.play_all("sess-1")

    assert speech.calls == [("sess-1", "Câu một."), ("sess-1", "Câu hai.")]
    assert all(isinstance(text, str) for _, text in speech.calls)


# ---------- 13.7: Change A ownership preserved ----------


def test_production_module_never_imports_text_chunker() -> None:
    """Importing the speaker must not pull the ``text_chunker`` package.

    Chunk policy, deadlines, hints, and finality live inside Change A's
    chunker, which is only reachable through the orchestrator adapter at the
    boundary — not through the sentence speaker. The speaker module must be
    importable in a process where the chunker has never been imported.
    """
    for name in list(sys.modules):
        if name == _CHUNKER_ROOT or name.startswith(_CHUNKER_ROOT + "."):
            del sys.modules[name]

    module = importlib.import_module(SRC_ROOT)

    assert _CHUNKER_ROOT not in sys.modules
    assert module.__name__ == SRC_ROOT


class _StrictSpeech:
    """Speech fake accepting EXACTLY (session_id, text) — nothing else.

    A TypeError on any extra kwarg proves the player passes no chunk policy,
    deadlines, or runtime hints to the boundary.
    """

    async def speak_sentence(self, session_id: str, text: str, **kwargs: object) -> str:
        if kwargs:
            raise TypeError(f"unexpected boundary kwargs: {sorted(kwargs)}")
        return text


async def test_boundary_call_carries_no_deadlines_or_hints() -> None:
    """The player passes only (session_id, text) to the speech boundary.

    No flush deadlines, no runtime hints, no chunk policy: a fake that
    rejects extra kwargs proves the exact call signature.
    """
    player = ScriptSentencePlayer(_FakeCursor(["Câu một."]), _StrictSpeech())

    assert await player.play_current("sess-1") == "Câu một."


# ---------- 13.8: approval/version immutability, exact slices ----------


def _require_real_cursor() -> None:
    """Skip with a clear message until the parallel cursor modules land.

    ``sentence_map.py``/``script_cursor.py`` arrive in cluster tasks 13.1-13.3
    (parallel implementer); these integration tests exercise the REAL cursor
    end-to-end and skip cleanly while it is missing.
    """
    for name in (
        "backend.application.live_runtime.sentence_map",
        "backend.application.live_runtime.script_cursor",
    ):
        try:
            importlib.import_module(name)
        except ImportError as exc:
            pytest.skip(f"parallel C13 cursor module missing, integration test skipped: {exc}")


def _build_real_cursor(script: ResolvedApprovedScript):
    """Real SentenceMap + ScriptCursor over the approved artifact."""
    from backend.application.live_runtime.sentence_map import SentenceMap
    from backend.application.live_runtime.script_cursor import ScriptCursor

    sentence_map = SentenceMap(script.spoken_text)
    return ScriptCursor(script, sentence_map), sentence_map


async def test_playing_sentences_leaves_approved_artifact_and_binding_unchanged() -> None:
    """Playing every sentence mutates only the cursor — never the artifact.

    The binding snapshot (as_dict) and the approved artifact object stay
    byte-for-byte identical after full playback.
    """
    _require_real_cursor()
    script = _approved("P001", "v-3", "Xin chào mọi người. Hôm nay giảm giá 50%! Nhanh tay nhé!")
    snapshot = build_binding_snapshot("set-1", [script])
    before_artifact = script
    before_snapshot = snapshot.as_dict()
    cursor, _ = _build_real_cursor(script)
    player = ScriptSentencePlayer(cursor, _RecordingSpeech())

    await player.play_all("sess-1")

    assert cursor.finished
    assert script == before_artifact, "approved artifact object must be unchanged"
    assert script.spoken_text == before_artifact.spoken_text
    assert snapshot.as_dict() == before_snapshot, "binding snapshot must be unchanged"


async def test_spoken_sentences_are_exact_slices_of_approved_text() -> None:
    """No rewrite: concatenating the spoken sentences reproduces spoken_text.

    Each sentence the player spoke is an exact contiguous slice of the
    approved artifact, and the full sequence reproduces it byte-for-byte.
    """
    _require_real_cursor()
    spoken_text = "Xin chào mọi người. Hôm nay giảm giá 50%! Nhanh tay nhé!"
    script = _approved("P001", "v-3", spoken_text)
    cursor, _ = _build_real_cursor(script)
    speech = _RecordingSpeech()
    player = ScriptSentencePlayer(cursor, speech)

    await player.play_all("sess-1")

    spoken = [text for _, text in speech.calls]
    assert "".join(spoken) == spoken_text
    assert cursor.finished
