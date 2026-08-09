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
  - Terminal invariant: normal exhaustion emits exactly one ``EofEvent``,
    but only after the producer-owned ``close()`` succeeded (or is absent);
    an upstream/cleanup error emits exactly one ``ErrorEvent``; cancellation
    emits no terminal event and never EOF. No terminal event is ever
    swallowed, and EOF is never emitted after an error.
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


#: Normal exhaustion emits exactly one ``EofEvent``; an error emits exactly one
#: ``ErrorEvent``; cancellation emits no terminal event. EOF is never emitted
#: after an error.
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
        generator: Optional[Any] = None
        try:
            generator = self._llm.stream_chunks(
                self._req,
                session_id=self._session_id,
                utterance_id=self._utterance_id,
            )
            if self._stop.is_set():
                # stop() won the race between invocation and the first next():
                # cancel path — close the never-iterated generator, no
                # terminal event.
                self._close_generator(generator)
                return
            for delta in generator:
                if self._stop.is_set():
                    # ``next()`` just returned, possibly after blocking in
                    # provider I/O: cancel path — the generator is suspended
                    # and this thread owns it, so close and exit, no terminal
                    # event.
                    self._close_generator(generator)
                    return
                self._put(DeltaEvent(text=delta.text, is_final=delta.is_final))
                if self._stop.is_set():
                    # stop() fired while the put was blocked on a full queue:
                    # cancel path — do not request the next upstream item
                    # (that would perform/block on one extra provider I/O);
                    # close the suspended generator, no terminal event.
                    self._close_generator(generator)
                    return
            if self._stop.is_set():
                # Cancellation observed between the last delta and exhaustion.
                return
            # Normal exhaustion: cleanup FIRST (EOF means clean), then decide
            # the terminal event. If close raises, EOF is withheld — the
            # consumer sees an ErrorEvent instead, exactly one terminal event.
            if self._close_generator(generator) is None:
                self._put(EofEvent())
        except GeneratorExit:
            # Only the producer itself can raise GeneratorExit, and it never
            # closes the generator while it is executing. Therefore this is
            # an unexpected upstream termination: if not cancelled, surface
            # it as the single terminal ErrorEvent (never EOF).
            if not self._stop.is_set():
                self._put(ErrorEvent(GeneratorExit()))
        except BaseException as exc:  # noqa: BLE001 - must cross threads verbatim
            # Upstream iteration/invocation raised. Preserve the original
            # exception as the single terminal ErrorEvent (unless cancelled).
            if not self._stop.is_set():
                self._put(ErrorEvent(exc))
            # Best-effort cleanup after the terminal decision. If close also
            # raises, do not replace the original; the only non-destructive
            # way to keep both is to annotate the original when supported.
            close_exc = self._close_generator(generator)
            if close_exc is not None:
                try:
                    exc.add_note(
                        f"generator close() failed: {type(close_exc).__name__}: {close_exc}"
                    )
                except Exception:  # noqa: BLE001 - annotation is best-effort
                    # add_note unavailable (or rejects) on this exception:
                    # keep the original untouched rather than mask it with a
                    # cleanup error or a second terminal event.
                    pass

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
    def _close_generator(generator: Optional[Any]) -> Optional[BaseException]:
        """Close the generator on the producer thread; return any close error.

        Only ever called by the producer thread, so no lock is needed and
        close-at-most-once falls out of the control flow (each ``_produce``
        exit path closes at most one generator it still owns). Returns the
        cleanup exception (or None) instead of swallowing it so the caller
        decides the terminal policy: EOF only after a clean close, error
        otherwise. ``BaseException`` is caught because a close that raises
        ``GeneratorExit``/``KeyboardInterrupt`` must be surfaced the same way.
        """
        if generator is None or not hasattr(generator, "close"):
            return None
        try:
            generator.close()
        except BaseException as exc:  # noqa: BLE001 - caller decides the policy
            return exc
        return None
