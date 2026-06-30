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
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

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

logger = logging.getLogger(__name__)


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
        orchestrator: StreamOrchestrator,
        lock_registry: SessionLockRegistry,
        cfg: Optional[CoordinatorConfig] = None,
    ) -> None:
        self._runtime = runtime
        self._orchestrator = orchestrator
        self._lock_registry = lock_registry
        self._cfg = cfg or CoordinatorConfig()
        self._queues: dict[str, ChatQueue] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stats: dict[str, _SessionStats] = {}
        self._active_score: dict[str, float] = {}
        self._embedder = None  # lazy

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
        task = asyncio.get_event_loop().create_task(
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
            director_comments.append(
                Comment(text=ic.text, embedding=vec, t=ic.ts)
            )
        state.add_comments(director_comments)

        # 4. Director decides.
        now = ds.now()
        state.phase_elapsed_sec = now
        decision = director.decide(state.rolling_comments, now=now)

        # 5. Skip?
        if decision.action in ("idle", "skip"):
            self._stats[session_id].skips += 1
            return

        await self._maybe_speak(session_id, decision)

    async def _maybe_speak(self, session_id: str, decision: Decision) -> None:
        """Attempt to acquire the lock and run the orchestrator for a decision."""
        st = self._stats.get(session_id)
        if st is None:
            return

        # 6-7. Lock arbitration.
        if self._lock_registry.is_locked(session_id):
            # Someone is speaking. Check interrupt eligibility.
            if decision.may_interrupt:
                existing_score = self._active_score.get(session_id, 0.0)
                new_score = getattr(decision, "score", 0.0) if hasattr(decision, "score") else 0.0
                # Use a heuristic score from the decision reason if no explicit score.
                if new_score == 0.0 and decision.reason:
                    # Extract score from reason string if present.
                    m = re.search(r"score=(\d+\.?\d*)", decision.reason)
                    if m:
                        new_score = float(m.group(1))

                if new_score > existing_score:
                    # Interrupt: cancel current speech.
                    await self._orchestrator.cancel(session_id)
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
        decision_score = 0.0
        if decision.reason:
            m = re.search(r"score=(\d+\.?\d*)", decision.reason)
            if m:
                decision_score = float(m.group(1))
        self._active_score[session_id] = decision_score

        # 9. Build the text to speak.
        text = decision.prompt or decision.text
        if not text:
            self._lock_registry.release(session_id)
            self._active_score.pop(session_id, None)
            st.skips += 1
            return

        # 10. Run the streaming pipeline.
        try:
            await self._orchestrator.run(session_id, text)
            st.decisions_emitted += 1
            st.last_decision_ts = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("orchestrator.run failed for session %s", session_id)
        finally:
            self._lock_registry.release(session_id)
            self._active_score.pop(session_id, None)


__all__ = ["DirectorCoordinator", "CoordinatorConfig"]
