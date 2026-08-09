"""Bounded typed-event controller around synchronous ``LLMEngine.stream_chunks``.

OpenSpec tasks 4.1 + 4.4: isolate the blocking sync generator behind a
bounded queue so the orchestrator consumer can honor a true latency deadline
while the upstream generator is stalled, apply backpressure without dropping/
duplicating/reordering deltas, and clean up the producer on EOF/error/cancel
without unsafe thread kills.

Threading contract:
  - ONE producer thread invokes, iterates, and closes the upstream
    generator/iterator. The controller consumer never calls upstream
    provider methods — ``close()`` included. A provider/custom-iterator
    ``close()`` may block indefinitely, so it must never run on the caller's
    thread; bounded cancellation only ever sets ``_stop`` and bounded-joins
    the producer.
  - The orchestrator consumer is the only party that mutates the
    ``TextChunker``; the controller never touches chunker state.
  - Blocking puts are stop-event responsive: a full queue never strands the
    producer when the consumer cancels.
  - Blocking gets are stop/wake-event responsive: ``get(None)`` re-checks the
    stop and an optional external cancel event with a bounded poll, so a
    stalled upstream generator cannot hang the consumer forever.
  - ``stop()`` is honest and bounded: set stop, join with a finite timeout.
    The producer owns the generator and closes it at every cooperative exit
    point where it has control (post-assignment stop, stop observed after
    ``next()`` returns, stop observed after ``_put``, abnormal-exit cleanup).
    If the provider is blocked in arbitrary I/O, ``stop()`` returns after
    ``JOIN_TIMEOUT_S`` and the daemon producer remains until the provider's
    finite I/O timeout returns; it then observes stop, closes on its own
    thread, and exits. Python cannot force-kill a thread, so providers must
    retain their own finite I/O timeouts at the engine seam (current backend
    outbound OpenAI client: httpx timeout=60s).
"""

from __future__ import annotations

import queue
import threading
import time
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
        """Request producer stop, then bounded-join the producer thread.

        Idempotent. Never runs arbitrary provider ``close()`` code on this
        thread: a provider/custom-iterator close may block forever, which
        would make cancellation unbounded. The producer alone owns the
        generator and closes it at its next cooperative checkpoint; if it is
        blocked in foreign I/O, the join times out and the daemon producer
        cleans up on its own thread once the provider I/O returns.
        """
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(JOIN_TIMEOUT_S)

    # -- producer internals -------------------------------------------------

    def _produce(self) -> None:
        # If stop() fired before this thread started, never invoke upstream.
        if self._stop.is_set():
            return
        generator = None
        try:
            generator = self._llm.stream_chunks(
                self._req,
                session_id=self._session_id,
                utterance_id=self._utterance_id,
            )
            if self._stop.is_set():
                # stop() won the race between invocation and the first next():
                # close the never-iterated generator and exit without
                # starting upstream.
                self._close_generator(generator)
                return
            for delta in generator:
                if self._stop.is_set():
                    # ``next()`` just returned, possibly after blocking in
                    # provider I/O: the generator is now suspended and this
                    # thread owns it, so it can be closed here.
                    self._close_generator(generator)
                    return
                self._put(DeltaEvent(text=delta.text, is_final=delta.is_final))
                # stop() may have fired while this put was blocked on a full
                # queue: do not request the next upstream item after a
                # cancellation (that would perform/block on one extra provider
                # I/O). The producer now owns the suspended generator, so it
                # can close it and return.
                if self._stop.is_set():
                    self._close_generator(generator)
                    return
            if not self._stop.is_set():
                self._put(EofEvent())
        except GeneratorExit:
            # A concurrent stop() closed the generator at a yield point (the
            # standard generator-cancellation path). Normal exit; no terminal
            # event: the stop was requested.
            return
        except BaseException as exc:  # noqa: BLE001 - must cross threads verbatim
            if not self._stop.is_set():
                # Terminal decision first: queue the ErrorEvent BEFORE any
                # potentially blocking close(), so the consumer's error path
                # is never hidden or hung by cleanup. Cleanup afterwards is
                # best-effort; if it raises, the ErrorEvent already won.
                self._put(ErrorEvent(exc))
            self._close_generator(generator)
            # A close that raised was deliberately swallowed: the terminal
            # decision was already made above, and close-at-most-once holds
            # (the generator is never touched again after this attempt).
            return

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

    @staticmethod
    def _close_generator(generator: Optional[Any]) -> None:
        """Close the generator on the producer thread, at most once per exit.

        The producer is the only thread that ever calls this, so no lock or
        state machine is needed: close-at-most-once falls out of the control
        flow (each ``_produce`` exit path closes at most one generator it
        still owns, and a generator closed by ``GeneratorExit`` is never
        closed again because the exit path returns before reaching the
        cleanup). A ``close()`` that raises (e.g. it blocks or fails) is
        swallowed here: the caller has already made its terminal decision,
        and cleanup is best-effort — it must not replace a queued
        ``ErrorEvent`` or create a second terminal event.
        """
        if generator is None or not hasattr(generator, "close"):
            return
        try:
            generator.close()
        except BaseException:  # noqa: BLE001 - cleanup is best-effort
            return
