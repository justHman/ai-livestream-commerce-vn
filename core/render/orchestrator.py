"""StreamOrchestrator — LLM stream -> TextChunker -> TTS stream -> backend stream (Task 8).

Wires the four streaming stages into one cancellable coordinator run. Used by
``/lite/say`` when the active backend is a streaming-capable renderer (mock +
future self-host). For cloud (``FullPipelineBackend``), the API keeps the
existing ``backend.say()`` path and does NOT use this coordinator.

Threading / async boundary:
  ``llm.stream_chunks``, ``tts.stream_audio`` and ``backend.stream_audio`` are
  SYNC ``Iterator`` generators. Iterating them directly in an async function
  would block the event loop for production engines (real LLM/TTS). To stay
  cooperative we run the whole sync pipeline in ONE thread via
  ``asyncio.to_thread(self._run_sync, ...)``. The sync function collects the
  ``VideoWindow`` objects into a list and returns them; the async ``run()``
  then drains the list into the async ``BoundedVideoQueue``. Cancel is
  signalled via a ``threading.Event`` checked by ``_run_sync`` between stages.

  For production heavy generators this is correct (sync work off the loop).
  For the stub/mock engines used in tests the thread overhead is negligible.

Cancel propagation:
  ``cancel(session_id)`` sets the ``threading.Event``; ``_run_sync`` checks it
  between LLM chunks, between TTS windows, and between backend windows, and
  breaks out early. The async ``run()`` then clears the bounded queue (drops
  any windows that were already queued but not yet consumed) so the caller
  does not play stale frames after a barge-in.

is_final propagation:
  The last ``TextChunk`` from the LLM is marked ``is_final=True`` (by the
  LLM adapter). The chunker's ``finalize()`` emits the final phrase chunk with
  ``is_final=True``. The TTS propagates ``is_final`` to the final
  ``AudioWindow`` of that phrase. The mock backend propagates
  ``AudioWindow.is_final`` to the ``VideoWindow``. The orchestrator does NOT
  override finality — it just forwards what each stage produces. The very
  last ``VideoWindow`` of the run therefore carries ``is_final=True``.
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from typing import Any, Callable, Optional

from ..llm.base import LLMEngine, LLMRequest
from ..stream.chunker import TextChunker
from ..tts.base import TTSEngine
from ..render.windows import TextChunk, VideoWindow
from .queue import BoundedVideoQueue, CoordinatorMetrics


# Default TextChunker config (mirrors AppConfig.text_chunk_* defaults). The
# orchestrator reads these from the ``config`` dict if provided, else uses
# these constants.
_DEFAULT_MIN_CHARS = 12
_DEFAULT_TARGET_CHARS = 40
_DEFAULT_MAX_CHARS = 80
_DEFAULT_FLUSH_TIMEOUT_MS = 350


class StreamOrchestrator:
    """Wires LLM -> TextChunker -> TTS -> backend into one cancellable run.

    Construct one orchestrator per /lite/say turn. ``run()`` is the entry
    point; ``cancel()`` is the barge-in hook called from /lite/interrupt.
    """

    def __init__(
        self,
        llm: LLMEngine,
        tts: TTSEngine,
        backend: Any,
        queue: BoundedVideoQueue,
        metrics: CoordinatorMetrics,
        config: Optional[dict] = None,
    ) -> None:
        self._llm = llm
        self._tts = tts
        self._backend = backend
        self._queue = queue
        self._metrics = metrics
        cfg = config or {}
        self._min_chars = int(cfg.get("text_chunk_min_chars", _DEFAULT_MIN_CHARS))
        self._target_chars = int(cfg.get("text_chunk_target_chars", _DEFAULT_TARGET_CHARS))
        self._max_chars = int(cfg.get("text_chunk_max_chars", _DEFAULT_MAX_CHARS))
        self._flush_timeout_ms = int(cfg.get("text_chunk_flush_timeout_ms", _DEFAULT_FLUSH_TIMEOUT_MS))
        # Cancel flag: checked by _run_sync between stages. threading.Event so
        # the sync thread can poll it without crossing the async boundary.
        self._cancel_event = threading.Event()
        self._running_session: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, session_id: str, text: str, system_prompt: Optional[str] = None) -> str:
        """Run one LLM->chunker->TTS->backend turn for ``session_id``.

        Puts each emitted ``VideoWindow`` into the bounded queue and records
        ``pipeline_total_ms`` on the first emission. Returns the full
        spoken text (concatenation of all LLM TextChunk texts).

        Note: the recorded ``pipeline_total_ms`` measures the FULL pipeline
        (sync thread produces ALL windows, then ``run()`` drains them into
        the queue and records on the first put). It is NOT true first-frame
        latency. True per-window streaming drain is post-P0.

        On cancel: stops emission, clears the bounded queue, and returns the
        text produced so far.
        """
        self._running_session = session_id
        self._cancel_event.clear()
        self._metrics.record_start()

        utterance_id = uuid.uuid4().hex
        req = LLMRequest.from_prompt(text, system_prompt=system_prompt)

        # Run the sync pipeline in a thread so the event loop stays free.
        # _run_sync returns (windows, spoken_text, dropped_count).
        windows, spoken, dropped = await asyncio.to_thread(
            self._run_sync,
            session_id,
            utterance_id,
            req,
        )

        # Drain the sync-collected windows into the async bounded queue. If a
        # drop happens here (queue overflow), update metrics accordingly.
        first_emitted = False
        for vw in windows:
            # Check cancel one more time before queuing (cancel may have
            # arrived while we were draining).
            if self._cancel_event.is_set():
                break
            ok = await self._queue.put(vw)
            if not ok:
                self._metrics.increment_dropped(1)
            if not first_emitted:
                self._metrics.record_first_frame()
                first_emitted = True
            self._metrics.update_queue_depth(self._queue.qsize())

        # If the sync side reported drops (e.g. internal overflow tracking),
        # fold them in.
        if dropped:
            self._metrics.increment_dropped(dropped)

        # On cancel, drain the queue so the consumer doesn't play stale frames.
        if self._cancel_event.is_set():
            self._queue.clear()
            self._metrics.update_queue_depth(0)

        self._running_session = None
        return spoken

    async def cancel(self, session_id: str) -> None:
        """Barge-in: stop the running turn for ``session_id`` and drain the queue.

        Idempotent. If no turn is running for the session, this is a no-op.
        """
        if self._running_session != session_id:
            # No active turn for this session on this orchestrator.
            return
        self._cancel_event.set()
        # Drain pending windows so the consumer doesn't play stale frames.
        self._queue.clear()
        self._metrics.update_queue_depth(0)

    # ------------------------------------------------------------------
    # Sync pipeline (runs in a thread)
    # ------------------------------------------------------------------

    def _run_sync(
        self,
        session_id: str,
        utterance_id: str,
        req: LLMRequest,
    ) -> tuple[list[VideoWindow], str, int]:
        """Run the full sync pipeline in one thread.

        Returns (windows, spoken_text, dropped_count). Checks
        ``self._cancel_event`` between stages and breaks out early on cancel.
        """
        windows: list[VideoWindow] = []
        spoken_parts: list[str] = []
        dropped = 0

        chunker = TextChunker(
            session_id=session_id,
            utterance_id=utterance_id,
            min_chars=self._min_chars,
            target_chars=self._target_chars,
            max_chars=self._max_chars,
            flush_timeout_ms=self._flush_timeout_ms,
        )

        # Stage 1: LLM stream -> TextChunker feed.
        # The LLM's stream_chunks yields TextChunks (token-sized deltas). Each
        # is fed to the chunker; the chunker emits phrase-sized TextChunks.
        # We collect phrase chunks for stage 2.
        phrase_chunks: list = []
        for tc in self._llm.stream_chunks(
            req, session_id=session_id, utterance_id=utterance_id
        ):
            if self._cancel_event.is_set():
                break
            spoken_parts.append(tc.text)
            # Feed the LLM TextChunk text into the chunker; collect any phrase
            # chunks it flushes. The chunker may emit zero or one chunk per
            # feed (punctuation / max / timeout conditions).
            phrase_chunks.extend(chunker.feed(tc.text))
            # Also poll timeout in case the LLM is momentarily quiet between
            # deltas (no-op when the timeout condition is not satisfied).
            phrase_chunks.extend(chunker.check_timeout())
        else:
            # LLM stream ended naturally (no cancel) -> finalize the chunker.
            if not self._cancel_event.is_set():
                phrase_chunks.extend(chunker.finalize())

        # Mark the LAST phrase chunk as final so TTS propagates is_final=True
        # to the final AudioWindow and the VideoWindow inherits it. The
        # chunker only emits is_final=True on a finalize() REMAINDER; when
        # every phrase flushed on punctuation the buffer is empty and
        # finalize() returns []. The orchestrator is the authority for "the
        # utterance is over" — stamp the last phrase chunk here.
        if phrase_chunks and not self._cancel_event.is_set():
            last = phrase_chunks[-1]
            if not last.is_final:
                phrase_chunks[-1] = TextChunk(
                    session_id=last.session_id,
                    utterance_id=last.utterance_id,
                    seq=last.seq,
                    text=last.text,
                    is_final=True,
                    id=last.id,
                )

        # Stage 2 + 3 + 4: TTS stream -> backend stream -> collect VideoWindows.
        for phrase in phrase_chunks:
            if self._cancel_event.is_set():
                break
            # TTS yields AudioWindows for this phrase.
            for audio_window in self._tts.stream_audio(
                phrase,
                session_id=session_id,
                utterance_id=utterance_id,
            ):
                if self._cancel_event.is_set():
                    break
                # Backend yields VideoWindows for this AudioWindow.
                for vw in self._backend.stream_audio(session_id, audio_window):
                    if self._cancel_event.is_set():
                        break
                    windows.append(vw)

        spoken = "".join(spoken_parts)
        return windows, spoken, dropped


__all__ = ["StreamOrchestrator"]
