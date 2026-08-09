"""Bounded typed-event controller around synchronous ``LLMEngine.stream_chunks``.

OpenSpec tasks 4.1 + 4.4: isolate the blocking sync generator behind a
bounded queue so the orchestrator consumer can honor a true latency deadline
while the upstream generator is stalled, apply backpressure without dropping/
duplicating/reordering deltas, and clean up the producer on EOF/error/cancel
without unsafe thread kills.

Threading contract:
  - ONE producer thread invokes and iterates ``stream_chunks`` and puts typed
    events (``DeltaEvent`` / ``EofEvent`` / ``ErrorEvent``) into a bounded
    FIFO. The controller consumer never calls upstream provider methods.
  - The orchestrator consumer is the only party that mutates the
    ``TextChunker``; the controller never touches chunker state.
  - Blocking puts are stop-event responsive: a full queue never strands the
    producer when the consumer cancels.
  - Blocking gets are stop/wake-event responsive: ``get(None)`` re-checks the
    stop and an optional external cancel event with a bounded poll, so a
    stalled upstream generator cannot hang the consumer forever.
  - Cleanup is honest and bounded: set stop, close the generator when it is
    safe (suspended at a yield), and ``join`` with a finite timeout. Python
    cannot force-kill a thread blocked in arbitrary provider/network I/O, so
    providers must retain their own finite I/O timeouts at the engine seam.
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Optional

__all__ = ["DeltaEvent", "EofEvent", "ErrorEvent", "LLMStreamController"]

#: Bounded delta-queue capacity. Slow TTS consumers exert backpressure on the
#: producer at this many undelivered LLM deltas; no unbounded text pile-up.
DEFAULT_QUEUE_MAXSIZE = 64

#: How often a full queue re-checks the stop event while backpressured.
_PUT_POLL_S = 0.05

#: Bounded producer join. A producer blocked in foreign I/O may outlive this;
#: the run still terminates (honest cleanup) and the engine seam is required
#: to provide finite I/O timeouts.
JOIN_TIMEOUT_S = 5.0

#: How often an indefinite ``get`` re-checks the stop/cancel state while
#: waiting for an event. 0.1 s keeps cancel latency negligible without a
#: busy-loop; the poll only runs while the queue is empty.
_GET_POLL_S = 0.1


@dataclass(frozen=True)
class DeltaEvent:
    """One LLM text delta (``text``) and whether upstream marked it final."""

    text: str
    is_final: bool


@dataclass(frozen=True)
class EofEvent:
    """Upstream generator exhausted normally; no error occurred."""


@dataclass(frozen=True)
class ErrorEvent:
    """Upstream generator raised ``exc``; the consumer must propagate it."""

    exc: BaseException


#: A typed ``EofEvent`` is never emitted after an ``ErrorEvent``: the producer
#: emits exactly one terminal event (EOF or error), unless stopped early.
StreamEvent = DeltaEvent | EofEvent | ErrorEvent


class LLMStreamController:
    """Produce typed stream events in one thread; consume with a bounded wait."""

    def __init__(
        self,
        llm: Any,
        req: Any,
        *,
        session_id: str,
        utterance_id: str,
        maxsize: int = DEFAULT_QUEUE_MAXSIZE,
    ) -> None:
        self._llm = llm
        self._req = req
        self._session_id = session_id
        self._utterance_id = utterance_id
        self._queue: queue.Queue[StreamEvent] = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._generator: Optional[Any] = None
        self._started = False

    # -- public API ---------------------------------------------------------

    def start(self) -> None:
        """Start the producer thread over ``llm.stream_chunks(...)``.

        Idempotence-guarded: starting an already-started controller raises
        ``RuntimeError`` so an accidental double-start cannot spawn two
        producers over one upstream generator.
        """
        if self._started:
            raise RuntimeError("LLMStreamController.start() called twice")
        self._started = True
        self._thread = threading.Thread(
            target=self._produce,
            name="llm-stream-producer",
            daemon=True,
        )
        self._thread.start()

    def get(
        self,
        timeout: Optional[float],
        cancel: Optional[threading.Event] = None,
    ) -> Optional[StreamEvent]:
        """Return the next event, or None after ``timeout`` seconds.

        ``timeout=None`` waits indefinitely (the caller uses it when the
        chunker buffer has no live deadline — empty or below the min-chars
        floor — so TTFT never fakes a deadline), but the wait is responsive
        to ``cancel``/stop: each poll re-checks both events, so a stalled
        upstream generator cannot hang the consumer forever.
        """
        if cancel is not None and cancel.is_set():
            return None
        return self._get(timeout, cancel)

    def stop(self) -> None:
        """Stop the producer, close the generator when safe, and join bounded.

        Idempotent. Closing a generator suspended at a yield point is the
        supported cancellation path; a generator mid-execution in foreign I/O
        cannot be closed, so the bounded join reports the leak instead of
        hanging the run.
        """
        self._stop.set()
        self._close_generator()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(JOIN_TIMEOUT_S)

    # -- producer internals -------------------------------------------------

    def _produce(self) -> None:
        try:
            generator = self._llm.stream_chunks(
                self._req,
                session_id=self._session_id,
                utterance_id=self._utterance_id,
            )
            # Stash so ``stop()`` can close it from the consumer thread when
            # it is suspended at a yield point.
            self._generator = generator
            for delta in generator:
                if self._stop.is_set():
                    return
                self._put(DeltaEvent(text=delta.text, is_final=delta.is_final))
            if not self._stop.is_set():
                self._put(EofEvent())
        except GeneratorExit:
            # stop() closed the generator at a yield point: normal exit.
            return
        except Exception as exc:  # noqa: BLE001 - must cross threads verbatim
            if not self._stop.is_set():
                self._put(ErrorEvent(exc))

    def _put(self, event: StreamEvent) -> None:
        while not self._stop.is_set():
            try:
                self._queue.put_nowait(event)
                return
            except queue.Full:
                self._stop.wait(_PUT_POLL_S)

    def _get(
        self, timeout: Optional[float], cancel: Optional[threading.Event]
    ) -> Optional[StreamEvent]:
        """Wait for the next event, re-checking stop/cancel each poll.

        An available event is returned immediately — the poll never delays a
        queued event; it only bounds how long an empty queue is waited on.
        """
        while True:
            if self._stop.is_set() or (cancel is not None and cancel.is_set()):
                return None
            if timeout is not None and timeout <= 0.0:
                return None
            wait = _GET_POLL_S if timeout is None else min(timeout, _GET_POLL_S)
            try:
                return self._queue.get(timeout=wait)
            except queue.Empty:
                if timeout is not None:
                    timeout -= wait

    def _close_generator(self) -> None:
        generator = self._generator
        if generator is None or not hasattr(generator, "close"):
            return
        try:
            generator.close()
        except (ValueError, RuntimeError):
            # Generator is mid-execution in the producer thread (blocked in
            # foreign I/O); closing from another thread is not supported.
            # The bounded join is the honest outcome.
            pass
