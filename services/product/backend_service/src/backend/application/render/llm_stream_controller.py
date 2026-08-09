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
import time
from dataclasses import dataclass
from enum import Enum
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


class _CloseState(Enum):
    """Generator-close coordination between consumer and producer threads.

    OPEN -> CLOSING -> CLOSED. A close that fails while the generator is
    mid-execution resets to OPEN so a later retry (from the consumer after the
    producer suspends, or from the producer itself) can still close it. The
    state lock is never held while invoking the custom ``close()`` code.
    """

    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"


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
        """Create a controller with a strictly bounded event queue.

        ``maxsize`` must be positive: ``queue.Queue(maxsize=0)`` is unbounded,
        which would violate the controller's bounded-queue contract.
        """
        if maxsize <= 0:
            raise ValueError(f"maxsize must be positive, got {maxsize}")
        self._llm = llm
        self._req = req
        self._session_id = session_id
        self._utterance_id = utterance_id
        self._queue: queue.Queue[StreamEvent] = queue.Queue(maxsize=maxsize)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._generator: Optional[Any] = None
        self._started = False
        self._close_state = _CloseState.OPEN
        #: Guards ``_close_state`` transitions; never held across ``close()``.
        self._close_lock = threading.Lock()

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
        # If the producer is currently suspended at a yield point (waiting on
        # the queue or on a stop-check re-read), close() from this thread
        # succeeds; if it is mid-execution in foreign I/O, close() fails fast
        # here and is retried by the producer once it re-checks the stop.
        self._close_generator()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(JOIN_TIMEOUT_S)

    # -- producer internals -------------------------------------------------

    def _produce(self) -> None:
        # If stop() fired before this thread started, never invoke upstream.
        if self._stop.is_set():
            return
        try:
            generator = self._llm.stream_chunks(
                self._req,
                session_id=self._session_id,
                utterance_id=self._utterance_id,
            )
            # Stash so ``stop()`` can close it from the consumer thread when
            # it is suspended at a yield point. If ``stop()`` won the race
            # between invocation and this assignment, close the never-iterated
            # generator and exit without starting upstream.
            self._generator = generator
            if self._stop.is_set():
                self._close_generator()
                return
            for delta in generator:
                if self._stop.is_set():
                    # ``next()`` just returned, possibly after blocking in
                    # provider I/O: the generator is now suspended and this
                    # thread owns it, so a close that failed from the
                    # consumer (mid-execution) can succeed from here.
                    self._close_generator()
                    return
                self._put(DeltaEvent(text=delta.text, is_final=delta.is_final))
                # stop() may have fired while this put was blocked on a full
                # queue: do not request the next upstream item after a
                # cancellation (that would perform/block on one extra provider
                # I/O). The producer now owns the suspended generator, so it
                # can close it safely and return.
                if self._stop.is_set():
                    self._close_generator()
                    return
            if not self._stop.is_set():
                self._put(EofEvent())
        except GeneratorExit:
            # Either stop() closed the generator at a yield point (normal
            # exit), or a concurrent close won a race while this body was
            # between yields. Both are normal exits: stop() already set the
            # closed flag when it ran close() successfully.
            return
        except BaseException as exc:  # noqa: BLE001 - must cross threads verbatim
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
        A finite timeout is a fixed deadline from ``time.monotonic()``: each
        poll waits ``min(remaining, _GET_POLL_S)`` so drift does not extend
        the deadline, and ``timeout=0`` performs one non-blocking get so an
        already-queued event still wins over a fabricated timeout. Cancelled
        controllers return None even with an event queued: once cancelled,
        queued deltas may be abandoned.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._stop.is_set() or (cancel is not None and cancel.is_set()):
                return None
            if deadline is None:
                wait = _GET_POLL_S
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    try:
                        return self._queue.get_nowait()
                    except queue.Empty:
                        return None
                wait = min(remaining, _GET_POLL_S)
            try:
                return self._queue.get(timeout=wait)
            except queue.Empty:
                continue

    def _close_generator(self) -> None:
        """Close the generator at most once, retrying a failed attempt.

        Close-at-most-once must hold across the consumer thread (``stop()``)
        and the producer thread (stop-check after ``_put``): a custom
        iterator's ``close()`` is not required to be repeatable. The
        OPEN/CLOSING/CLOSED state machine under ``_close_lock`` arbitrates
        concurrent attempts without holding the lock across the arbitrary
        close() code (which would risk deadlock if it touches the controller).
        A failed close (generator mid-execution in the producer thread)
        resets to OPEN so a later retry can still close it; repeated
        successful closes remain at-most-once.
        """
        generator = self._generator
        # A None generator (stop before any invocation) has nothing to close,
        # and the guard must not be burned so a later stop() after a real
        # assignment can still close it.
        if generator is None or not hasattr(generator, "close"):
            return
        with self._close_lock:
            if self._close_state is not _CloseState.OPEN:
                return
            self._close_state = _CloseState.CLOSING
        try:
            generator.close()
        except (ValueError, RuntimeError):
            # Generator is mid-execution in the producer thread (blocked in
            # foreign I/O); closing from another thread is not supported.
            # Reset so a retry (consumer after the producer suspends, or the
            # producer itself at its next stop-check) can still close it.
            with self._close_lock:
                if self._close_state is _CloseState.CLOSING:
                    self._close_state = _CloseState.OPEN
            return
        with self._close_lock:
            if self._close_state is _CloseState.CLOSING:
                self._close_state = _CloseState.CLOSED
