"""DirectorCoordinator — background tick loop draining ChatQueue into Director decisions (Phase B).

Bridges continuous chat ingestion to the Director FSM + StreamOrchestrator
pipeline. One coordinator instance serves ALL sessions; each session has its
own ChatQueue and background ``asyncio.Task`` running ``_tick_loop``.

Lifecycle (called by future /lite/attach and /lite/stop):
  start(session_id, products) -> attach runtime, create queue, launch tick task
  stop(session_id)            -> cancel task, drop queue, cancel orchestrator
  ingest(session_id, text, author, ts?) -> push one comment into the queue

The tick loop:
  Every ``tick_ms`` ms:
    1. drain_window -> fresh comments
    2. embed new-only (cache by comment id in state.embeddings_cache)
    3. convert to Director Comment objects, merge into state via add_comments
    4. director.decide(state) -> Decision
    5. if skip -> continue
    6. lock arbitration: if speaking, check interrupt eligibility
    7. orchestrator.run(session_id, text) -> streaming pipeline
    8. release lock

The loop NEVER halts on exceptions (except CancelledError). Errors are logged
and the next tick proceeds.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional

from .catalog import Product
from .chat_queue import ChatQueue, IncomingComment
from .cluster import Comment
from .director import Decision, Director
from .embedder import build_embedder
from .hooks import HookPool
from .config import StreamConfig
from .runtime import DirectorRuntime
from .state import ProductState, StreamState

from ..render.orchestrator import StreamOrchestrator
from ..render.locks import SessionLockRegistry
from ..render.queue import BoundedVideoQueue, CoordinatorMetrics

if TYPE_CHECKING:
    # Avoid a circular import at runtime: core.api.v1 imports DirectorCoordinator
    # at module load, so importing ControlHub eagerly here would fail. The hub
    # is only used through its async ``emit(session_id, event)`` method.
    from ..api.v1 import ControlHub

logger = logging.getLogger(__name__)


def _decision_to_event(decision: Decision) -> dict:
    """Project a Director Decision to a frontend-friendly WS event payload.

    Drops non-serializable fields and keeps only what the UI ticker needs.
    """
    return {
        "action": decision.action,
        "product": decision.product_id,
        "field": decision.field,
        "may_interrupt": decision.may_interrupt,
        "reason": decision.reason,
    }


@dataclass
class CoordinatorConfig:
    """Tunable coordinator knobs."""

    tick_ms: int = 300
    window_sec: float = 75.0
    may_interrupt_default: bool = False


@dataclass
class _SessionStats:
    """Per-session coordinator counters."""

    decisions_emitted: int = 0
    skips: int = 0
    interrupts: int = 0
    last_decision_ts: Optional[float] = None


class DirectorCoordinator:
    """Background coordinator that drains per-session ChatQueues into Director decisions.

    Public surface:
      start(session_id, products, cfg?, hooks?) -> None
      stop(session_id) -> None
      ingest(session_id, text, author, ts?) -> IncomingComment
      stats(session_id) -> dict
    """

    def __init__(
        self,
        runtime: DirectorRuntime,
        llm: Any,
        tts: Any,
        backend: Any,
        chunker_config: Optional[dict] = None,
        lock_registry: Optional[SessionLockRegistry] = None,
        cfg: Optional[CoordinatorConfig] = None,
        hub: Optional["ControlHub"] = None,
        orchestrator_registry: Optional[dict] = None,
        max_queue_windows: int = 5,
        pg_store: Any = None,
    ) -> None:
        self._runtime = runtime
        # Factory inputs for building a FRESH StreamOrchestrator + queue +
        # metrics per _maybe_speak() call. Sharing one orchestrator across
        # concurrent sessions corrupts per-turn state (cancel_event, queue,
        # metrics, running_session) — session A's cancel() would stop
        # session B's pipeline. Building per-call mirrors /lite/say's
        # _streaming_say pattern in core/api/v1.py.
        self._llm = llm
        self._tts = tts
        self._backend = backend
        self._chunker_config = dict(chunker_config or {})
        self._max_queue_windows = max_queue_windows
        self._lock_registry = lock_registry or SessionLockRegistry()
        self._cfg = cfg or CoordinatorConfig()
        self._hub = hub
        # Shared dict (typically V1Deps.orchestrators) the coordinator writes
        # to while speaking so the continuous MJPEG endpoint can find the active
        # queue and serve utterance frames. When None, registration is skipped
        # (tests that do not exercise MJPEG).
        self._orchestrator_registry = orchestrator_registry
        self._queues: dict[str, ChatQueue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stats: dict[str, _SessionStats] = {}
        self._active_score: dict[str, float] = {}
        self._embedder = None  # lazy
        # Optional Postgres runtime store (durable rows). None/disabled -> no
        # persistence. Fire-and-forget: a failure must never break the speak loop.
        self._pg_store = pg_store

    async def _emit(self, session_id: str, event: dict) -> None:
        """Send a WS event via the ControlHub if one is wired. No-op otherwise."""
        if self._hub is None:
            return
        try:
            await self._hub.emit(session_id, event)
        except Exception:
            logger.debug("hub.emit failed for %s", session_id, exc_info=True)

    def _register_speaking(
        self, session_id: str, orchestrator: StreamOrchestrator, queue: BoundedVideoQueue
    ) -> None:
        """Publish the active orchestrator+queue so MJPEG can drain utterance frames.

        Per-call: the orchestrator+queue are fresh for each _maybe_speak()
        invocation, so the registry always points at the in-flight turn's
        queue (not a shared long-lived one).
        """
        if self._orchestrator_registry is None:
            return
        self._orchestrator_registry[session_id] = {
            "orchestrator": orchestrator,
            "queue": queue,
        }

    def _unregister_speaking(self, session_id: str) -> None:
        if self._orchestrator_registry is None:
            return
        self._orchestrator_registry.pop(session_id, None)

    def _get_embedder(self):
        if self._embedder is None:
            self._embedder = build_embedder()
        return self._embedder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        session_id: str,
        products: list[Product],
        cfg: Optional[StreamConfig] = None,
        hooks: Optional[HookPool] = None,
    ) -> None:
        """Attach the DirectorRuntime, create a ChatQueue, and launch the tick loop.

        If the session is already started, this is a no-op (idempotent).
        """
        if session_id in self._tasks:
            return  # already running

        # Attach the Director via the existing runtime (embeds catalog, builds state).
        if not self._runtime.has(session_id):
            self._runtime.attach(session_id, products, cfg=cfg, hooks=hooks)

        self._queues[session_id] = ChatQueue(session_id)
        self._stats[session_id] = _SessionStats()
        task = asyncio.create_task(
            self._tick_loop(session_id),
            name=f"coordinator-tick-{session_id}",
        )
        self._tasks[session_id] = task

    def stop(self, session_id: str) -> None:
        """Cancel the tick task, drop the queue, detach runtime.

        If the orchestrator is currently speaking for this session, cancel it.
        Idempotent.
        """
        task = self._tasks.pop(session_id, None)
        if task is not None and not task.done():
            task.cancel()

        self._queues.pop(session_id, None)
        self._stats.pop(session_id, None)
        self._active_score.pop(session_id, None)
        self._runtime.detach(session_id)
        self._lock_registry.drop(session_id)

    def ingest(
        self,
        session_id: str,
        text: str,
        author: str,
        ts: Optional[float] = None,
    ) -> IncomingComment:
        """Push one comment into the session's ChatQueue.

        Raises KeyError if the session has not been started.
        """
        queue = self._queues.get(session_id)
        if queue is None:
            raise KeyError(f"No active coordinator session: {session_id}")
        return queue.put(text, author, ts=ts)

    def stats(self, session_id: str) -> dict:
        """Diagnostic snapshot for a session."""
        queue = self._queues.get(session_id)
        st = self._stats.get(session_id)
        q_stats = queue.stats() if queue else {"pending": 0, "oldest_ms_ago": None}
        last_ms = None
        if st and st.last_decision_ts is not None:
            last_ms = round((time.monotonic() - st.last_decision_ts) * 1000, 1)
        return {
            "queue": q_stats,
            "decisions_emitted": st.decisions_emitted if st else 0,
            "last_decision_ms_ago": last_ms,
            "skips": st.skips if st else 0,
            "interrupts": st.interrupts if st else 0,
        }

    def has(self, session_id: str) -> bool:
        """True if a coordinator session is active for this session_id."""
        return session_id in self._tasks

    # ------------------------------------------------------------------
    # Background tick loop
    # ------------------------------------------------------------------

    async def _tick_loop(self, session_id: str) -> None:
        """Infinite tick loop; cancelled externally via ``stop()``."""
        tick_sec = self._cfg.tick_ms / 1000.0
        try:
            while True:
                await asyncio.sleep(tick_sec)
                try:
                    await self._tick_once(session_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "coordinator tick error for session %s (continuing)",
                        session_id,
                    )
        except asyncio.CancelledError:
            logger.debug("coordinator tick loop cancelled for %s", session_id)

    async def _tick_once(self, session_id: str) -> None:
        """One tick: drain window, embed, decide, maybe speak."""
        queue = self._queues.get(session_id)
        if queue is None:
            return

        ds = self._runtime._sessions.get(session_id)
        if ds is None:
            return

        director: Director = ds.director
        state: StreamState = director.state
        embedder = self._get_embedder()

        # 1. Get fresh comments from the rolling window.
        fresh = queue.drain_window(self._cfg.window_sec)
        if not fresh:
            # Even with no comments, run decide() so phase timers and hooks fire.
            now = ds.now()
            state.phase_elapsed_sec = now
            decision = director.decide(state.rolling_comments, now=now)
            if decision.action != "idle":
                await self._maybe_speak(session_id, decision)
            return

        # 2. Embed only new comments (not already in cache).
        new_only = [c for c in fresh if c.id not in state.embeddings_cache]
        if new_only:
            texts = [c.text for c in new_only]
            vecs = embedder.encode(texts)
            for c, v in zip(new_only, vecs):
                state.embeddings_cache[c.id] = list(v)

        # 3. Convert to Director Comment objects and merge into state.
        director_comments = []
        for ic in fresh:
            vec = state.embeddings_cache.get(ic.id)
            if vec is None:
                # Should not happen, but guard.
                vec = embedder.encode([ic.text])[0]
                state.embeddings_cache[ic.id] = list(vec)
            director_comments.append(Comment(text=ic.text, embedding=vec, t=ic.ts))
        state.add_comments(director_comments)

        # 4. Director decides.
        now = ds.now()
        state.phase_elapsed_sec = now
        decision = director.decide(state.rolling_comments, now=now)

        # Emit the decision to the WS hub (frontend ticker). Best-effort; the
        # loop continues regardless.
        await self._emit(
            session_id,
            {
                "type": "director.decision",
                **_decision_to_event(decision),
            },
        )

        # 5. Skip?
        if decision.action in ("idle", "skip"):
            self._stats[session_id].skips += 1
            return

        await self._maybe_speak(session_id, decision)

    async def _maybe_speak(self, session_id: str, decision: Decision) -> None:
        """Attempt to acquire the lock and run the orchestrator for a decision.

        Builds a FRESH ``StreamOrchestrator`` + ``BoundedVideoQueue`` +
        ``CoordinatorMetrics`` for this call so concurrent sessions do not
        corrupt each other's per-turn state (cancel_event, queue, metrics,
        running_session). Mirrors the per-turn pattern in
        ``core/api/v1.py::_streaming_say``.
        """
        st = self._stats.get(session_id)
        if st is None:
            return

        # 6-7. Lock arbitration.
        if self._lock_registry.is_locked(session_id):
            # Someone is speaking. Check interrupt eligibility.
            if decision.may_interrupt:
                existing_score = self._active_score.get(session_id, 0.0)
                new_score = float(decision.score)

                if new_score > existing_score:
                    # Interrupt: cancel current speech. The active
                    # orchestrator for this session is the one registered in
                    # the orchestrator_registry (set by a previous
                    # _maybe_speak call still in flight). Cancel through the
                    # registry entry rather than a shared self._orchestrator.
                    if self._orchestrator_registry is not None:
                        entry = self._orchestrator_registry.get(session_id)
                        if entry is not None:
                            active_orch: StreamOrchestrator = entry["orchestrator"]
                            await active_orch.cancel(session_id)
                    self._lock_registry.release(session_id)
                    st.interrupts += 1
                    # Short backoff so the cancel propagates.
                    await asyncio.sleep(self._cfg.tick_ms / 1000.0)
                else:
                    st.skips += 1
                    return
            else:
                st.skips += 1
                return

        # 8. Try to acquire the lock.
        ok = self._lock_registry.try_acquire(session_id)
        if not ok:
            st.skips += 1
            return

        # Stash the decision score for interrupt comparison.
        decision_score = float(decision.score)
        self._active_score[session_id] = decision_score

        # 9. Build the text to speak.
        text = decision.prompt or decision.text
        if not text:
            self._lock_registry.release(session_id)
            self._active_score.pop(session_id, None)
            st.skips += 1
            return

        # 10. Build a FRESH orchestrator+queue+metrics for this turn and run it.
        queue = BoundedVideoQueue(max_size=self._max_queue_windows)
        metrics = CoordinatorMetrics()
        orchestrator = StreamOrchestrator(
            llm=self._llm,
            tts=self._tts,
            backend=self._backend,
            queue=queue,
            metrics=metrics,
            config=self._chunker_config,
        )
        self._register_speaking(session_id, orchestrator, queue)
        try:
            await self._emit(
                session_id,
                {
                    "type": "coordinator.speak_started",
                    "text": text,
                    "utterance": text,
                    "action": decision.action,
                    "product": decision.product_id,
                },
            )
            await orchestrator.run(session_id, text)
            st.decisions_emitted += 1
            st.last_decision_ts = time.monotonic()
            # M3: persist the Director decision to the runtime DB (fire-and-forget).
            await self._persist_decision(session_id, decision, text)
            # M3: coverage + cursor advance after proactive speak.
            self._after_speak(session_id, decision, text)
            await self._emit(
                session_id,
                {
                    "type": "coordinator.speak_finished",
                    "text": text,
                    "utterance": text,
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("orchestrator.run failed for session %s", session_id)
        finally:
            self._lock_registry.release(session_id)
            self._active_score.pop(session_id, None)
            self._unregister_speaking(session_id)

    async def _persist_decision(self, session_id: str, decision: Decision, speech: str) -> None:
        """Persist a Director decision row to the runtime DB (fire-and-forget).

        No-op when pg_store is None/disabled. A failure is logged at debug and
        swallowed — a broken runtime DB must never stall the speak loop.
        """
        if self._pg_store is None or not getattr(self._pg_store, "enabled", False):
            return
        try:
            phase = None
            ds = self._runtime._sessions.get(session_id)
            if ds is not None:
                phase = ds.director.state.cursor.phase if ds.director.state.cursor else None
            await self._pg_store.insert_director_decision(
                session_id,
                decision.action,
                product_id=decision.product_id,
                score=decision.score,
                phase=phase,
                utterance=speech,
                reason=decision.reason,
            )
        except Exception:
            logger.debug("pg insert_director_decision failed for %s", session_id, exc_info=True)

    def _after_speak(self, session_id: str, decision: Decision, speech: str) -> None:
        """Update covered_points + advance talking_point_idx on proactive speak."""
        ds = self._runtime._sessions.get(session_id)
        if ds is None:
            return
        state: StreamState = ds.director.state
        plan = state.run_plan
        product_id = decision.product_id
        key_points: list[str] = []
        if plan is not None and product_id:
            selling = getattr(plan, "selling", None)
            if selling is None and isinstance(plan, dict):
                selling = plan.get("selling") or []
            for sp in selling or []:
                pid = sp.product_id if hasattr(sp, "product_id") else sp.get("product_id")
                if pid == product_id:
                    ksp = (
                        sp.key_selling_points
                        if hasattr(sp, "key_selling_points")
                        else sp.get("key_selling_points") or []
                    )
                    key_points = list(ksp)
                    break
        if key_points and speech:
            try:
                from .coverage import mark_coverage

                thr = float(os.environ.get("COVERAGE_MATCH_THRESHOLD", "0.75"))
                prev = state.covered_points.get(product_id) or set()
                covered = mark_coverage(
                    self._get_embedder(),
                    speech,
                    key_points,
                    threshold=thr,
                    already_covered=prev,
                )
                state.mark_product_covered(product_id, covered)
            except Exception:
                logger.debug("coverage update failed", exc_info=True)
        # Advance cursor only for proactive (non-reactive) actions.
        if decision.action in (
            "speak_hook",
            "introduce_product",
            "answer_fact",
            "close",
        ) or (decision.action == "answer_cluster" and not decision.may_interrupt):
            n = len(key_points) if key_points else 1
            state.advance_talking_point(n)
            # Keep cursor phase in sync with FSM phase.
            state.cursor.phase = state.phase.value
            state.cursor.product_idx = state.current_product_index


__all__ = ["DirectorCoordinator", "CoordinatorConfig"]
