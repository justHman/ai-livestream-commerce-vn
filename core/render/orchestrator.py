"""StreamOrchestrator — LLM stream -> TextChunker -> TTS stream -> backend stream.

Wires the four streaming stages into one cancellable coordinator run. Used by
``/lite/say`` and by ``DirectorCoordinator`` when the active renderer is a
``StreamingAvatarBackend`` (mock + future self-host). For cloud
(``FullPipelineBackend``), the API keeps the existing backend.say() path and does
NOT use this orchestrator.

Threading / async boundary:
  ``llm.stream_chunks``, ``tts.stream_audio`` and ``backend.stream_audio`` are
  SYNC ``Iterator`` generators. Iterating them directly in an async function
  would block the event loop for production engines (real LLM/TTS). To stay
  cooperative, the sync pipeline runs in one worker thread via
  ``asyncio.to_thread(self._run_sync, ...)``.

  Phase E changes the bridge from "collect all windows, then drain" to true
  streaming-drain: the worker pushes each ``VideoWindow`` into a thread-safe
  ``queue.Queue`` as soon as the backend yields it; async ``run()`` drains that
  bridge concurrently into ``BoundedVideoQueue``. ``pipeline_total_ms`` is now
  stamped when the FIRST VideoWindow is put into the async queue, so it is a
  real first-window latency proxy instead of total pipeline time.

Cancel propagation:
  ``cancel(session_id)`` sets a ``threading.Event``; ``_run_sync`` checks it
  between LLM chunks, between TTS windows, and between backend windows, and
  breaks out early. The async ``run()`` drains the bounded queue on cancel so
  consumers do not play stale frames after a barge-in.

is_final propagation:
  The last phrase chunk is stamped ``is_final=True`` by the orchestrator so TTS
  propagates it to the final ``AudioWindow`` and the backend propagates it to the
  final ``VideoWindow``.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from ..llm.base import LLMEngine, LLMRequest
from ..render.windows import AudioWindow, TextChunk, VideoWindow
from ..stream.chunker import TextChunker
from ..tts.base import TTSEngine
from .queue import BoundedVideoQueue, CoordinatorMetrics

# Default TextChunker config (mirrors AppConfig.text_chunk_* defaults). The
# orchestrator reads these from the ``config`` dict if provided, else uses these
# constants.
_DEFAULT_MIN_CHARS = 12
_DEFAULT_TARGET_CHARS = 40
_DEFAULT_MAX_CHARS = 80
_DEFAULT_FLUSH_TIMEOUT_MS = 350
_BRIDGE_SENTINEL = object()
AudioWindowCallback = Callable[[AudioWindow], Awaitable[None]]


class StreamOrchestrator:
    """Wires LLM -> TextChunker -> TTS -> streaming backend into one run.

    Construct one orchestrator per /lite/say turn or coordinator session.
    ``run()`` is the entry point; ``cancel()`` is the barge-in hook called from
    /lite/interrupt or DirectorCoordinator interrupt arbitration.
    """

    def __init__(
        self,
        llm: LLMEngine,
        tts: TTSEngine,
        backend: Any,
        queue: BoundedVideoQueue,
        metrics: CoordinatorMetrics,
        config: dict | None = None,
        audio_window_callback: AudioWindowCallback | None = None,
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
        self._cancel_event = threading.Event()
        self._running_session: str | None = None
        self._audio_window_callback = audio_window_callback
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run(self, session_id: str, text: str, system_prompt: str | None = None) -> str:
        """Run one LLM->chunker->TTS->backend turn for ``session_id``.

        Puts each emitted ``VideoWindow`` into the bounded queue as soon as the
        sync worker produces it. Returns the full spoken text (concatenation of
        all LLM TextChunk text). ``CoordinatorMetrics.record_first_frame()`` is
        called on the first successful queue put.
        """
        self._running_session = session_id
        self._loop = asyncio.get_running_loop()
        self._cancel_event.clear()
        self._metrics.record_start()

        utterance_id = uuid.uuid4().hex
        req = LLMRequest.from_prompt(text, system_prompt=system_prompt)
        bridge: queue.Queue[VideoWindow | object] = queue.Queue()

        worker = asyncio.create_task(asyncio.to_thread(
            self._run_sync,
            session_id,
            utterance_id,
            req,
            bridge,
        ))

        first_emitted = False
        try:
            while True:
                item = await asyncio.to_thread(bridge.get)
                if item is _BRIDGE_SENTINEL:
                    break
                if self._cancel_event.is_set():
                    break
                vw = item
                ok = await self._queue.put(vw)  # type: ignore[arg-type]
                if not ok:
                    self._metrics.increment_dropped(1)
                if not first_emitted:
                    self._metrics.record_first_frame()
                    first_emitted = True
                self._metrics.update_queue_depth(self._queue.qsize())

            spoken, dropped = await worker
            if dropped:
                self._metrics.increment_dropped(dropped)

            if self._cancel_event.is_set():
                self._queue.clear()
                self._metrics.update_queue_depth(0)

            return spoken
        finally:
            if not worker.done():
                self._cancel_event.set()
                await worker
            self._running_session = None
            self._loop = None

    async def cancel(self, session_id: str) -> None:
        """Barge-in: stop the running turn for ``session_id`` and drain the queue.

        Idempotent. If no turn is running for the session, this is a no-op.
        """
        if self._running_session != session_id:
            return
        self._cancel_event.set()
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
        bridge: queue.Queue[VideoWindow | object],
    ) -> tuple[str, int]:
        """Run the sync generators and push VideoWindows to ``bridge``.

        Returns ``(spoken_text, dropped_count)``. The sentinel is always pushed
        so async ``run()`` never blocks forever if a sync stage raises.
        """
        spoken_parts: list[str] = []
        dropped = 0

        try:
            chunker = TextChunker(
                session_id=session_id,
                utterance_id=utterance_id,
                min_chars=self._min_chars,
                target_chars=self._target_chars,
                max_chars=self._max_chars,
                flush_timeout_ms=self._flush_timeout_ms,
            )

            # Stage 1: LLM stream -> TextChunker feed. We still need phrase
            # chunks before TTS can run, but we do NOT collect video windows;
            # every rendered window is bridged immediately downstream.
            phrase_chunks: list[TextChunk] = []
            for tc in self._llm.stream_chunks(
                req, session_id=session_id, utterance_id=utterance_id
            ):
                if self._cancel_event.is_set():
                    break
                spoken_parts.append(tc.text)
                phrase_chunks.extend(chunker.feed(tc.text))
                phrase_chunks.extend(chunker.check_timeout())
            else:
                if not self._cancel_event.is_set():
                    phrase_chunks.extend(chunker.finalize())

            # Stamp finality onto the last phrase chunk even when punctuation
            # flushing left no final remainder for chunker.finalize().
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

            # Stage 2 + 3 + 4: TTS stream -> backend stream -> bridge windows.
            for phrase in phrase_chunks:
                if self._cancel_event.is_set():
                    break
                for audio_window in self._tts.stream_audio(
                    phrase,
                    session_id=session_id,
                    utterance_id=utterance_id,
                ):
                    if self._cancel_event.is_set():
                        break
                    if self._audio_window_callback is not None:
                        loop = self._loop
                        if loop is None:
                            raise RuntimeError("orchestrator event loop is unavailable")
                        asyncio.run_coroutine_threadsafe(
                            self._audio_window_callback(audio_window), loop
                        ).result()
                    for vw in self._backend.stream_audio(session_id, audio_window):
                        if self._cancel_event.is_set():
                            break
                        bridge.put(vw)

            return "".join(spoken_parts), dropped
        finally:
            bridge.put(_BRIDGE_SENTINEL)


__all__ = ["StreamOrchestrator"]
