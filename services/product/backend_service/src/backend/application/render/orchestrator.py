"""StreamOrchestrator — LLM stream -> TextChunker -> TTS stream -> backend stream.

Copied from ``core/render/orchestrator.py`` (COPY-DON'T-IMPORT, OpenSpec
1.21) so the canonical backend service is self-contained. Imports point at
the canonical sibling packages (``llm.engines.base``, ``tts.engines.base``,
``avatar.engines.windows``, ``avatar.queue``) and the canonical local
``backend.application.text_chunker``.

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

Exactly-once finality (tasks 6.1-6.5):
  The TTS seam is per-call finality (tts/engines/base.py marks the LAST
  window of every synthesis call final and ignores ``TextChunk.is_final``).
  The orchestrator normalizes per-call finals into exactly one
  utterance-level final using one-window lookahead: each yielded
  ``AudioWindow`` is held back until the next window of the same TTS call
  arrives, then delivered with the NEXT window's finality (and the
  next-window span). On call completion, the final window is delivered with
  exactly one normal-success final flag — set only when the utterance
  actually completed (EOF reached and, when the chunker had no textual
  remainder, the last already-emitted logical chunk is the one stamped
  final). Errors and cancellation exit through their own paths and never
  stamp the normal final. No empty terminal audio/video window is ever
  fabricated. ``decision_reason`` survives every reconstruction.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from backend.application.contracts.llm_engines import LLMEngine, LLMRequest
from backend.application.contracts.tts_engines import TTSEngine
from .llm_stream_controller import (
    DEFAULT_QUEUE_MAXSIZE,
    EofEvent,
    ErrorEvent,
    LLMStreamController,
)
from .windows import AudioWindow, VideoWindow
from .queue import BoundedVideoQueue, CoordinatorMetrics

from ..text_chunker import ChunkDecisionReason, TelemetryCollector, TextChunk, TextChunker

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
        telemetry: TelemetryCollector | None = None,
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
        self._flush_timeout_ms = int(
            cfg.get("text_chunk_flush_timeout_ms", _DEFAULT_FLUSH_TIMEOUT_MS)
        )
        self._flush_timeout_s = self._flush_timeout_ms / 1000.0
        self._cancel_event = threading.Event()
        self._running_session: str | None = None
        self._audio_window_callback = audio_window_callback
        self._telemetry = telemetry
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

        worker = asyncio.create_task(
            asyncio.to_thread(
                self._run_sync,
                session_id,
                utterance_id,
                req,
                bridge,
            )
        )

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

    async def speak_verbatim(self, session_id: str, text: str) -> str:
        """Run TTS->backend for exact text without invoking the LLM."""
        self._running_session = session_id
        self._loop = asyncio.get_running_loop()
        self._cancel_event.clear()
        self._metrics.record_start()
        utterance_id = uuid.uuid4().hex
        bridge: queue.Queue[VideoWindow | object] = queue.Queue()
        worker = asyncio.create_task(
            asyncio.to_thread(
                self._speak_verbatim_sync,
                session_id,
                utterance_id,
                text,
                bridge,
            )
        )
        first_emitted = False
        try:
            while True:
                item = await asyncio.to_thread(bridge.get)
                if item is _BRIDGE_SENTINEL:
                    break
                if self._cancel_event.is_set():
                    break
                ok = await self._queue.put(item)  # type: ignore[arg-type]
                if not ok:
                    self._metrics.increment_dropped(1)
                if not first_emitted:
                    self._metrics.record_first_frame()
                    first_emitted = True
                self._metrics.update_queue_depth(self._queue.qsize())
            await worker
            if self._cancel_event.is_set():
                self._queue.clear()
                self._metrics.update_queue_depth(0)
            return text
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

    def _speak_verbatim_sync(
        self,
        session_id: str,
        utterance_id: str,
        text: str,
        bridge: queue.Queue[VideoWindow | object],
    ) -> None:
        try:
            phrase = TextChunk(
                session_id=session_id,
                utterance_id=utterance_id,
                seq=0,
                text=text,
                is_final=True,
            )
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
                for video_window in self._backend.stream_audio(session_id, audio_window):
                    if self._cancel_event.is_set():
                        break
                    bridge.put(video_window)
        finally:
            bridge.put(_BRIDGE_SENTINEL)

    def _run_sync(
        self,
        session_id: str,
        utterance_id: str,
        req: LLMRequest,
        bridge: queue.Queue[VideoWindow | object],
    ) -> tuple[str, int]:
        """Stream each complete LLM phrase into TTS as soon as it is available.

        The LLM stream runs in a dedicated producer thread; this consumer owns
        the ``TextChunker`` exclusively and waits on the controller with a
        deadline computed only from ``chunker.buffer_started_at``, so a
        stalled upstream generator can still fire a latency flush. Producer
        errors propagate through the error path; no EOF is emitted after an
        error, and the chunker is never stamped normal-final on error/cancel.

        Finality: the TTS seam is per-call final (tts/engines/base.py), so
        phrase finality does NOT cross the boundary. ``render_phrase`` stamps
        exactly one utterance-level final — the last audio window of the
        final phrase — only on true normal completion; error/cancel paths
        never stamp it.
        """
        spoken_parts: list[str] = []
        # Windows whose utterance-finality is not yet known, held until the
        # run's completion path decides. Bounded: at most ONE deadline-flush
        # phrase (whose lastness is unknowable until the next event) is ever
        # held; feed phrases are always deliverable immediately. Released
        # non-final on error/cancel, final on normal completion.
        held_windows: list[AudioWindow] = []

        def release_held(is_final: bool) -> None:
            """Deliver the held windows with the decided finality.

            Exactly-once rule: only the LAST held window may carry the normal
            final (``is_final`` True); every earlier one is emitted
            non-final. On error/cancel ``is_final`` is False for all.
            """
            while held_windows:
                window = held_windows.pop(0)
                self._deliver_audio_window(
                    window,
                    is_final=is_final and not held_windows,
                    text_span=window.text_span,
                    session_id=session_id,
                    bridge=bridge,
                )

        def render_phrase(
            phrase: TextChunk,
            *,
            hold_last: bool = False,
            stamp_chunk_final: bool = False,
        ) -> None:
            """Synthesize one phrase; deliver all but the call's last window.

            The TTS seam marks the last window of EVERY call final
            (tts/engines/base.py), so per-call finals are meaningless across
            calls. One-window lookahead within the call: each window is
            delivered carrying the NEXT window's finality (non-final), and
            the call's last window is either delivered now — final only when
            ``stamp_chunk_final`` (the utterance's final phrase) — or held
            when ``hold_last`` (a deadline-flush phrase whose lastness is
            unknown). ``stamp_chunk_final`` also marks the phrase chunk
            itself is_final=True for the canonical seam (decision_reason
            preserved).
            """
            if stamp_chunk_final:
                phrase = TextChunk(
                    session_id=phrase.session_id,
                    utterance_id=phrase.utterance_id,
                    seq=phrase.seq,
                    text=phrase.text,
                    is_final=True,
                    id=phrase.id,
                    decision_reason=phrase.decision_reason,
                )
            pending: AudioWindow | None = None
            # Task 7.2: time real synthesis/streaming lazily — the generator
            # is consumed in place (cancel stays responsive), duration is
            # accumulated per window, and the record lands on both the normal
            # and error completions of the loop (cancel path skips it).
            t0 = time.monotonic()
            audio_duration_ms = 0
            try:
                for audio_window in self._tts.stream_audio(
                    phrase,
                    session_id=session_id,
                    utterance_id=utterance_id,
                ):
                    if self._cancel_event.is_set():
                        return
                    audio_duration_ms += audio_window.duration_ms
                    if pending is not None:
                        self._deliver_audio_window(
                            pending,
                            is_final=False,
                            text_span=pending.text_span,
                            session_id=session_id,
                            bridge=bridge,
                        )
                    pending = audio_window
            except BaseException:
                # Mid-call TTS error: the partial window is still real audio —
                # deliver it non-final, then propagate (no fabricated final).
                if pending is not None:
                    self._deliver_audio_window(
                        pending,
                        is_final=False,
                        text_span=pending.text_span,
                        session_id=session_id,
                        bridge=bridge,
                    )
                self._record_tts_timing(time.monotonic() - t0, audio_duration_ms)
                raise
            self._record_tts_timing(time.monotonic() - t0, audio_duration_ms)
            if pending is not None:
                if hold_last:
                    held_windows.append(pending)
                else:
                    self._deliver_audio_window(
                        pending,
                        is_final=stamp_chunk_final,
                        text_span=pending.text_span,
                        session_id=session_id,
                        bridge=bridge,
                    )

        def render_final_batch(phrases: list[TextChunk]) -> None:
            """Render the utterance-final phrase batch; exactly one final."""
            release_held(is_final=False)
            for i, phrase in enumerate(phrases):
                render_phrase(phrase, stamp_chunk_final=i == len(phrases) - 1)

        controller: LLMStreamController | None = None
        normal_complete = False
        cancelled = False
        try:
            chunker = TextChunker(
                session_id=session_id,
                utterance_id=utterance_id,
                min_chars=self._min_chars,
                target_chars=self._target_chars,
                max_chars=self._max_chars,
                flush_timeout_ms=self._flush_timeout_ms,
                telemetry=self._telemetry,
            )
            controller = LLMStreamController(
                self._llm,
                req,
                session_id=session_id,
                utterance_id=utterance_id,
                maxsize=DEFAULT_QUEUE_MAXSIZE,
            )
            controller.start()
            try:
                while True:
                    if self._cancel_event.is_set():
                        cancelled = True
                        break
                    event = controller.get(
                        self._remaining_deadline(chunker),
                        cancel=self._cancel_event,
                    )
                    if event is None:
                        if not self._cancel_event.is_set():
                            phrases = chunker.flush(reason=ChunkDecisionReason.LATENCY_DEADLINE)
                            for i, phrase in enumerate(phrases):
                                render_phrase(phrase, hold_last=i == len(phrases) - 1)
                        continue
                    if isinstance(event, EofEvent):
                        break
                    if isinstance(event, ErrorEvent):
                        raise event.exc
                    # DeltaEvent
                    spoken_parts.append(event.text)
                    if event.is_final:
                        normal_complete = True
                        final_phrases = chunker.feed(event.text)
                        final_phrases.extend(chunker.finalize())
                        if final_phrases:
                            render_final_batch(final_phrases)
                        else:
                            # Completion with no new textual remainder: the
                            # held flush phrase (if any) is the last one.
                            release_held(is_final=True)
                        break
                    phrases = chunker.feed(event.text)
                    if phrases:
                        release_held(is_final=False)
                        for phrase in phrases:
                            render_phrase(phrase)

                if not cancelled and not normal_complete:
                    final_phrases = chunker.finalize()
                    if final_phrases:
                        render_final_batch(final_phrases)
                    else:
                        release_held(is_final=True)
            except BaseException:
                # Any abnormal exit (upstream/TTS/callback error) must
                # release the held windows NON-final, then propagate — never
                # stamp a normal-success final.
                release_held(is_final=False)
                raise
            if cancelled:
                release_held(is_final=False)
            return "".join(spoken_parts), 0
        finally:
            # The sentinel must ALWAYS reach the bridge: if it is skipped the
            # async drain in ``run()`` blocks forever in ``bridge.get``. Put it
            # in a nested finally so a raising cleanup still propagates (never
            # swallowed) while the consumer is unblocked.
            try:
                if controller is not None:
                    controller.stop()
            finally:
                bridge.put(_BRIDGE_SENTINEL)

    def _record_tts_timing(self, elapsed_s: float, audio_duration_ms: int) -> None:
        """Record one synthesis call's timing (no-op without a collector).

        ``elapsed_s`` spans the whole lazy stream consumption, so the
        first-audio latency equals the synthesis wall time on this
        non-streaming seam (synthesize once, then split windows).
        """
        if self._telemetry is not None:
            self._telemetry.record_tts_timing(elapsed_s * 1000.0, audio_duration_ms)

    def _deliver_audio_window(
        self,
        audio_window: AudioWindow,
        *,
        is_final: bool,
        text_span: str | None,
        session_id: str,
        bridge: queue.Queue[VideoWindow | object],
    ) -> None:
        """Deliver one AudioWindow to the callback + backend with given finality.

        A frozen dataclass cannot be mutated, so the finality/span stamping
        reconstructs the window (same id, same PCM). ``decision_reason``
        lives on the TextChunk, not the audio window, so no reason is lost.
        """
        if is_final != audio_window.is_final or text_span != audio_window.text_span:
            audio_window = AudioWindow(
                session_id=audio_window.session_id,
                utterance_id=audio_window.utterance_id,
                seq=audio_window.seq,
                sample_rate=audio_window.sample_rate,
                duration_ms=audio_window.duration_ms,
                pcm=audio_window.pcm,
                audio_path=audio_window.audio_path,
                text_span=text_span,
                is_final=is_final,
                id=audio_window.id,
            )
        if self._cancel_event.is_set():
            return
        if self._audio_window_callback is not None:
            loop = self._loop
            if loop is None:
                raise RuntimeError("orchestrator event loop is unavailable")
            asyncio.run_coroutine_threadsafe(
                self._audio_window_callback(audio_window), loop
            ).result()
        for video_window in self._backend.stream_audio(session_id, audio_window):
            if self._cancel_event.is_set():
                return
            bridge.put(video_window)

    def _remaining_deadline(self, chunker: TextChunker) -> float | None:
        """Seconds until the buffer latency deadline, or None when no deadline.

        The deadline is measured from ``buffer_started_at`` only: an empty
        buffer (long LLM TTFT, flushed remainder, or a sub-min buffer below
        the quality floor) waits without a fake deadline. ``flush_timeout_ms``
        of 0 is honored as an immediate flush once text is buffered.

        Min-quality guard: a non-empty buffer below ``min_chars`` returns
        None — the latency deadline never forces a sub-min flush. The
        acceptance contract is that sub-min timeout continues buffering until
        min is reached (or finalization/cancellation/hard cap), so the
        timeout only fires once the buffer has at least ``min_chars``.
        """
        started = chunker.buffer_started_at
        if started is None:
            return None
        if len(chunker.buffered_text) < self._min_chars:
            return None
        remaining = self._flush_timeout_s - chunker.buffer_age_ms / 1000.0
        return max(0.0, remaining)


__all__ = ["StreamOrchestrator"]
