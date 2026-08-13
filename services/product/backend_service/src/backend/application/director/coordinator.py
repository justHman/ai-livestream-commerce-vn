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
import copy
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from backend.application.entity.models import EntityDocument

from .comment_buffer import ChatQueue, IncomingComment
from .clustering import Comment, cluster_comments
from .decision import Decision, Director
from .embeddings import embedder_status
from .hooks import HookPool
from .config import StreamConfig
from .scoring import rank_clusters
from .session_context import DirectorRuntime
from .routing import route_comment
from .state import StreamState

from backend.application.render.locks import SessionLockRegistry
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics

from ..render.orchestrator import StreamOrchestrator, StreamingControllerConfig
from ..text_chunker import FixedChunkPolicyConfig

if TYPE_CHECKING:
    # The hub is only used through its async ``emit(session_id, event)``
    # method, so the import stays lazy (TYPE_CHECKING) to avoid cycles.
    from backend.api.v1.hub import ControlHub

logger = logging.getLogger(__name__)


def _decision_to_event(decision: Decision) -> dict:
    """Delegate to the canonical events module (OpenSpec 1.21)."""
    from .events import decision_to_event

    return decision_to_event(decision)


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
    director_cycles: int = 0
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
        fixed_config: Optional[FixedChunkPolicyConfig] = None,
        controller_config: Optional[StreamingControllerConfig] = None,
        # NOTE: ``chunker_config`` is a legacy dict shim kept for signature
        # compatibility only — production wiring passes typed configs
        # (fixed_config/controller_config).
        lock_registry: Optional[SessionLockRegistry] = None,
        cfg: Optional[CoordinatorConfig] = None,
        hub: Optional["ControlHub"] = None,
        orchestrator_registry: Optional[dict] = None,
        max_queue_windows: int = 5,
        pg_store: Any = None,
        audio_window_callback: Any = None,
        completed_history_size: int = 10,
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
        self._fixed_config = fixed_config or FixedChunkPolicyConfig()
        self._controller_config = controller_config or StreamingControllerConfig()
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
        self._playback_tasks: dict[str, asyncio.Task] = {}
        self._prepare_tasks: dict[str, set[asyncio.Task]] = {}
        self._decision_locks: dict[str, asyncio.Lock] = {}
        self._playback_events: dict[str, asyncio.Event] = {}
        self._stats: dict[str, _SessionStats] = {}
        self._active_score: dict[str, float] = {}
        self._last_tick: dict[str, float] = {}
        self._decision_queue: dict[str, deque[Decision]] = {}
        self._speech_queue: dict[str, deque[Decision]] = {}
        self._current_speech: dict[str, Decision] = {}
        self._completed_speech: dict[str, dict] = {}
        self._completed_history: dict[str, deque[dict]] = {}
        self._completed_history_size = completed_history_size
        self._activated: set[str] = set()
        # Optional Postgres runtime store (durable rows). None/disabled -> no
        # persistence. Fire-and-forget: a failure must never break the speak loop.
        self._pg_store = pg_store
        self._audio_window_callback = audio_window_callback

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

    @property
    def _embedder(self):
        """Compatibility seam backed by DirectorRuntime's shared embedder."""
        return self._runtime.embedder

    @_embedder.setter
    def _embedder(self, value) -> None:
        self._runtime._embedder = value

    def _get_embedder(self):
        return self._runtime.embedder

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        session_id: str,
        products: list[EntityDocument],
        cfg: Optional[StreamConfig] = None,
        hooks: Optional[HookPool] = None,
        *,
        activated: bool = True,
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
        self._decision_queue[session_id] = deque()
        self._speech_queue[session_id] = deque()
        self._prepare_tasks[session_id] = set()
        self._decision_locks[session_id] = asyncio.Lock()
        self._playback_events[session_id] = asyncio.Event()
        self._completed_history[session_id] = deque(maxlen=self._completed_history_size)
        self._stats[session_id] = _SessionStats()
        if activated:
            self._activated.add(session_id)
        try:
            ds = self._runtime.get_session(session_id)
        except KeyError:
            ds = None
        self._last_tick[session_id] = ds.now() if ds is not None else 0.0
        self._tasks[session_id] = asyncio.create_task(
            self._tick_loop(session_id),
            name=f"coordinator-tick-{session_id}",
        )
        self._playback_tasks[session_id] = asyncio.create_task(
            self._playback_loop(session_id),
            name=f"coordinator-playback-{session_id}",
        )

    def stop(self, session_id: str) -> None:
        """Cancel the tick task, drop the queue, detach runtime.

        If the orchestrator is currently speaking for this session, cancel it.
        Idempotent.
        """
        current = self._current_speech.get(session_id)
        if current is not None:
            current.is_cancelled = True
        tasks = [
            self._tasks.pop(session_id, None),
            self._playback_tasks.pop(session_id, None),
            *self._prepare_tasks.pop(session_id, set()),
        ]
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        entry = (
            self._orchestrator_registry.get(session_id)
            if self._orchestrator_registry is not None
            else None
        )
        if entry is not None:
            asyncio.create_task(entry["orchestrator"].cancel(session_id))

        self._queues.pop(session_id, None)
        self._decision_queue.pop(session_id, None)
        self._speech_queue.pop(session_id, None)
        self._decision_locks.pop(session_id, None)
        self._playback_events.pop(session_id, None)
        self._current_speech.pop(session_id, None)
        self._completed_speech.pop(session_id, None)
        self._completed_history.pop(session_id, None)
        self._stats.pop(session_id, None)
        self._active_score.pop(session_id, None)
        self._last_tick.pop(session_id, None)
        self._activated.discard(session_id)
        self._runtime.detach(session_id)
        self._lock_registry.drop(session_id)

    def stop_all(self) -> None:
        """Cancel every active coordinator session."""
        for session_id in list(self._tasks):
            self.stop(session_id)

    def update_catalog(self, session_id: str, products: list[EntityDocument]) -> None:
        """Refresh catalog and invalidate work created before Re-attach."""
        session = self._runtime.get_session(session_id)
        session.director.catalog = {product.id: product for product in products}
        self._invalidate_queued(session_id, reason="attach_revision")

    def update_runtime_config(self, session_id: str, values: dict) -> dict:
        """Validate config, then cancel work prepared under the prior revision."""
        result = self._runtime.update_runtime_config(session_id, values)
        self._invalidate_queued(session_id, reason="config_revision")
        return result

    def _record_cancelled(self, session_id: str, decision: Decision, reason: str) -> None:
        if decision.is_cancelled:
            return
        decision.is_cancelled = True
        if session_id not in self._stats or not self._runtime.has(session_id):
            return
        cancelled = {
            **self._speech_item(decision, "cancelled_stale"),
            "cancellation_reason": reason,
        }
        self._completed_speech[session_id] = cancelled
        self._completed_history.setdefault(
            session_id, deque(maxlen=self._completed_history_size)
        ).append(cancelled)

    def _invalidate_queued(self, session_id: str, reason: str) -> None:
        for queue in (
            self._decision_queue.get(session_id),
            self._speech_queue.get(session_id),
        ):
            if queue is None:
                continue
            while queue:
                self._record_cancelled(session_id, queue.popleft(), reason)
        prepare_tasks = self._prepare_tasks.get(session_id, set())
        self._prepare_tasks[session_id] = set()
        current_task = asyncio.current_task()
        for task in prepare_tasks:
            if task is not current_task and not task.done():
                task.cancel()
        event = self._playback_events.get(session_id)
        if event is not None:
            event.set()

    async def interrupt(self, session_id: str) -> str:
        """Cancel active playback and invalidate every queued/prepared turn."""
        token = self._runtime.invalidate_generation(session_id)
        current = self._current_speech.get(session_id)
        if current is not None:
            self._record_cancelled(session_id, current, "interrupt")
        self._invalidate_queued(session_id, reason="interrupt")
        entry = (
            self._orchestrator_registry.get(session_id)
            if self._orchestrator_registry is not None
            else None
        )
        if entry is not None:
            await entry["orchestrator"].cancel(session_id)
        await asyncio.to_thread(self._backend.interrupt, session_id)
        return token

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
        comment = queue.put(text, author, ts=ts)
        self._activated.add(session_id)
        return comment

    @staticmethod
    def _speech_item(decision: Decision, state: str = "queued") -> dict:
        """Delegate to the canonical events module (OpenSpec 1.21)."""
        from .events import speech_item

        return speech_item(decision, state)

    def update_traffic(
        self,
        session_id: str,
        viewer_count: Optional[int] = None,
        msg_rate: Optional[float] = None,
    ) -> None:
        try:
            ds = self._runtime.get_session(session_id)
        except KeyError as exc:
            raise KeyError(f"No active coordinator session: {session_id}") from exc
        if viewer_count is not None:
            ds.director.state.traffic.viewer_count = viewer_count
        if msg_rate is not None:
            ds.director.state.traffic.msg_rate = msg_rate

    def speech_plan(self, session_id: str) -> dict:
        current = self._current_speech.get(session_id)
        pending = self._speech_queue.get(session_id) or ()
        try:
            ds = self._runtime.get_session(session_id)
        except KeyError:
            ds = None
        products = ds.director.state.products if ds is not None else []
        current_index = ds.director.state.current_product_index if ds is not None else -1

        def product_item(index: int) -> Optional[dict]:
            if not (0 <= index < len(products)):
                return None
            product = products[index]
            return {"product_id": product.product_id, "name": product.name}

        history = list(self._completed_history.get(session_id) or ())
        completed = history[-1] if history else self._completed_speech.get(session_id)
        return {
            "current": self._speech_item(current, "processing") if current else None,
            "upcoming": [self._speech_item(item) for item in pending],
            "completed": completed,
            "completed_history": history,
            "current_product": product_item(current_index),
            "next_product": product_item(current_index + 1),
        }

    def stats(self, session_id: str) -> dict:
        """Canonical diagnostic snapshot with temporary legacy aliases."""
        snapshot_at = time.time()
        queue = self._queues.get(session_id)
        st = self._stats.get(session_id)
        try:
            ds = self._runtime.get_session(session_id)
        except KeyError:
            ds = None
        window_sec = (
            ds.director.cfg.selection_window_sec if ds is not None else self._cfg.window_sec
        )
        q_stats = (
            queue.stats(window_sec=window_sec, now=snapshot_at)
            if queue
            else {
                "received_total": 0,
                "buffered_comments": 0,
                "active_comments": 0,
                "oldest_ms_ago": None,
                "pending": 0,
                "total_put": 0,
            }
        )
        speech = self.speech_plan(session_id)
        last_ms = None
        if st and st.last_decision_ts is not None:
            last_ms = round((time.monotonic() - st.last_decision_ts) * 1000, 1)
        completed_speeches = st.decisions_emitted if st else 0
        revisions = (
            {
                "profile_revision": ds.profile_revision,
                "catalog_revision": ds.catalog_revision,
                "config_revision": ds.config_revision,
                "generation_token": ds.generation_token,
            }
            if ds is not None
            else {
                "profile_revision": 0,
                "catalog_revision": 0,
                "config_revision": 0,
                "generation_token": "",
            }
        )
        result = {
            "snapshot_at": snapshot_at,
            **revisions,
            "accepted_snapshot": dict(ds.accepted_snapshot) if ds is not None else {},
            "pivot_state": (
                {
                    "active": ds.director.state.cursor.pivot_active,
                    "product_id": ds.director.state.cursor.pivot_product_id,
                    "checkpoint_product_id": ds.director.state.cursor.checkpoint_product_id,
                    "queued_products": list(ds.director.state.cursor.pivot_queue),
                }
                if ds is not None
                else {}
            ),
            "answer_cache": (
                {
                    "keys": len(ds.director.state.answer_variants),
                    "variants": sum(
                        len(variants) for variants in ds.director.state.answer_variants.values()
                    ),
                }
                if ds is not None
                else {"keys": 0, "variants": 0}
            ),
            "received_total": q_stats["received_total"],
            "buffered_comments": q_stats["buffered_comments"],
            "active_comments": q_stats["active_comments"],
            "director_cycles": st.director_cycles if st else 0,
            "active_decision": speech["current"],
            "queued_decisions": len(speech["upcoming"]),
            "queued_decisions_detail": speech["upcoming"],
            "completed_speeches": completed_speeches,
            "completed_speech_history": speech["completed_history"],
            "queue": q_stats,
            "speech_queue": speech,
            "last_decision_ms_ago": last_ms,
            "skips": st.skips if st else 0,
            "interrupts": st.interrupts if st else 0,
            # Temporary migration aliases.
            "decisions_emitted": completed_speeches,
        }
        return result

    def cluster_snapshot(self, session_id: str) -> dict:
        """Return the exact active cluster set used by the session Director."""
        queue = self._queues.get(session_id)
        if queue is None:
            raise KeyError(f"No active coordinator session: {session_id}")
        ds = self._runtime.get_session(session_id)
        director = ds.director
        state = director.state
        cfg = director.cfg
        snapshot_at = time.time()
        queue_stats = queue.stats(
            window_sec=cfg.selection_window_sec,
            now=snapshot_at,
        )
        active_comments = [
            comment
            for comment in state.rolling_comments
            if ds.now() - comment.t <= cfg.selection_window_sec
        ]
        clusters = cluster_comments(active_comments, merge_threshold=cfg.cluster_merge_threshold)
        ranked = rank_clusters(clusters, state, cfg, now=ds.now())
        unanswered = [
            item.cluster
            for item in ranked
            if not any(
                member_id in state.answered_comments for member_id in item.cluster.member_ids
            )
        ]
        status = embedder_status(ds.embedder)
        sorted_clusters = sorted(clusters, key=lambda cluster: cluster.size, reverse=True)
        return {
            "session_id": session_id,
            "snapshot_at": snapshot_at,
            **queue_stats,
            "selection_window_sec": cfg.selection_window_sec,
            "cluster_merge_threshold": cfg.cluster_merge_threshold,
            "embedder_name": status["name"],
            "embedder_status": "degraded" if status["degraded"] else "ready",
            "embedder": status,
            "total_comments": len(active_comments),
            "cluster_count": len(clusters),
            "total_clusters": len(clusters),
            "multi_comment_clusters": sum(cluster.size > 1 for cluster in clusters),
            "singleton_clusters": sum(cluster.size == 1 for cluster in clusters),
            "actionable_clusters": len(ranked),
            "unanswered_clusters": len(unanswered),
            "clusters": [
                {
                    "size": cluster.size,
                    "newest_t": cluster.newest_t,
                    "product_id": cluster.product_id,
                    "category": cluster.category,
                    "intent": cluster.intent,
                    "actionable": cluster.actionable,
                    "members": list(cluster.members),
                }
                for cluster in sorted_clusters
            ],
        }

    def has(self, session_id: str) -> bool:
        """True if a coordinator session is active for this session_id."""
        return session_id in self._tasks

    def _advance_timers(self, session_id: str, now: float, state: StreamState) -> None:
        """Increment all three elapsed counters by delta since last tick."""
        prev = self._last_tick.get(session_id, now)
        delta = max(0.0, now - prev)
        # Preserve the high-water mark after a backward clock jump.
        self._last_tick[session_id] = max(now, prev)
        state.phase_elapsed_sec += delta
        state.product_elapsed_sec += delta
        state.sec_since_relevant_msg += delta

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
        """Ingest continuously, then fill decision/preparation queues."""
        queue = self._queues.get(session_id)
        if queue is None or session_id not in self._activated:
            return
        ds = self._runtime._sessions.get(session_id)
        if ds is None:
            return
        director: Director = ds.director
        state: StreamState = director.state
        embedder = self._get_embedder()
        fresh = queue.drain_window(self._cfg.window_sec)
        new_only = [comment for comment in fresh if comment.id not in state.embeddings_cache]
        if new_only:
            vecs = await asyncio.to_thread(embedder.encode, [comment.text for comment in new_only])
            for comment, vector in zip(new_only, vecs):
                state.embeddings_cache[comment.id] = list(vector)
        director_now = ds.now()
        wall_now = time.time()
        routed = []
        current = state.current_product()
        for incoming in new_only:
            vector = state.embeddings_cache[incoming.id]
            routed.append(
                route_comment(
                    Comment(
                        text=incoming.text,
                        embedding=vector,
                        t=director_now - max(0.0, wall_now - incoming.ts),
                        id=incoming.id,
                    ),
                    ds.catalog,
                    current.product_id if current is not None else None,
                )
            )
        state.add_comments(routed)
        now = ds.now()
        self._advance_timers(session_id, now, state)
        await self._fill_prepared(session_id)

    def _projected_director(self, session_id: str) -> Director:
        ds = self._runtime.get_session(session_id)
        projection = copy.deepcopy(ds.director)
        completed_ids = {
            item.get("turn_id") for item in self._completed_history.get(session_id, ())
        }
        current = self._current_speech.get(session_id)
        if current is not None and current.turn_id not in completed_ids:
            projection.mark_spoken(current)
        for decision in self._decision_queue.get(session_id, ()):
            projection.mark_spoken(decision)
        for decision in self._speech_queue.get(session_id, ()):
            projection.mark_spoken(decision)
        return projection

    async def _fill_prepared(self, session_id: str) -> None:
        ds = self._runtime._sessions.get(session_id)
        if ds is None:
            return
        async with self._decision_locks[session_id]:
            depth = ds.director.cfg.prepared_turn_depth
            prepared = self._speech_queue[session_id]
            in_preparation = len(self._prepare_tasks[session_id])
            missing = max(0, depth - len(prepared) - in_preparation)
            if missing == 0:
                return
            projection = self._projected_director(session_id)
            for _ in range(missing):
                now = ds.now()
                decision = projection.decide(projection.state.rolling_comments, now=now)
                self._stats[session_id].director_cycles += 1
                if decision.action in ("idle", "skip"):
                    self._stats[session_id].skips += 1
                    break
                decision.revision_token = self._runtime.current_generation_token(session_id)
                decision.latency_spans["decision"] = {
                    "start": time.monotonic(),
                    "end": time.monotonic(),
                }
                self._decision_queue[session_id].append(decision)
                projection.mark_spoken(decision)
                await self._emit(
                    session_id,
                    {"type": "director.decision", **_decision_to_event(decision)},
                )
                task = asyncio.create_task(
                    self._prepare_turn(session_id, decision),
                    name=f"coordinator-prepare-{session_id}-{decision.turn_id}",
                )
                task._stage2_decision = decision  # type: ignore[attr-defined]
                self._prepare_tasks[session_id].add(task)
                task.add_done_callback(
                    lambda finished, sid=session_id: self._prepare_tasks.get(sid, set()).discard(
                        finished
                    )
                )

    async def _prepare_turn(self, session_id: str, decision: Decision) -> None:
        started = time.monotonic()
        decision.latency_spans["preparation"] = {"start": started, "end": started}
        try:
            ds = self._runtime.get_session(session_id)
            while True:
                queue = self._decision_queue.get(session_id)
                if queue is None or decision not in queue:
                    return
                if queue[0] is decision:
                    break
                await asyncio.sleep(0)
            decision.prompt_layers = self._runtime.prompt_layers(session_id, decision)
            can_prepare = getattr(self._llm, "name", "none") != "none"
            if decision.prompt is not None and can_prepare:
                from .decision_preparation import generate_variants

                variant_count = (
                    ds.director.cfg.answer_cache_variants
                    if decision.action in ("answer_fact", "answer_cluster")
                    else 1
                )
                prepared = await asyncio.to_thread(
                    generate_variants,
                    self._llm,
                    decision.prompt,
                    ds.system_prompt,
                    variant_count=variant_count,
                    session_id=session_id,
                    utterance_id=decision.turn_id,
                )
                decision.prepared_variants = prepared.variants
                decision.prepared_script = prepared.script
            elif decision.prompt is None and decision.prepared_script is None:
                decision.prepared_script = decision.text
            if decision.revision_token != self._runtime.current_generation_token(session_id):
                self._record_cancelled(session_id, decision, "generation_revision")
                return
            queue = self._decision_queue.get(session_id)
            if queue is None or not queue or queue[0] is not decision:
                self._record_cancelled(session_id, decision, "decision_queue_invalidated")
                return
            queue.popleft()
            decision.latency_spans["preparation"]["end"] = time.monotonic()
            self._speech_queue[session_id].append(decision)
            self._playback_events[session_id].set()
        except asyncio.CancelledError:
            self._record_cancelled(session_id, decision, "preparation_cancelled")
            raise
        except Exception as exc:
            decision.is_cancelled = False
            failed = {
                **self._speech_item(decision, "failed"),
                "error": type(exc).__name__,
            }
            self._completed_speech[session_id] = failed
            self._completed_history.setdefault(
                session_id, deque(maxlen=self._completed_history_size)
            ).append(failed)
            queue = self._decision_queue.get(session_id)
            if queue is not None:
                try:
                    queue.remove(decision)
                except ValueError:
                    pass
            self._activated.discard(session_id)
            self._invalidate_queued(session_id, reason="terminal_preparation_failure")
            logger.exception("turn preparation failed session=%s", session_id)
            await self._emit(
                session_id,
                {
                    "type": "coordinator.terminal_failure",
                    "turn_id": decision.turn_id,
                    "state": "failed",
                    "error": type(exc).__name__,
                },
            )
        finally:
            queue = self._decision_queue.get(session_id)
            if queue is not None:
                try:
                    queue.remove(decision)
                except ValueError:
                    pass

    async def _playback_loop(self, session_id: str) -> None:
        event = self._playback_events[session_id]
        try:
            while True:
                await event.wait()
                event.clear()
                queue = self._speech_queue.get(session_id)
                while queue:
                    decision = queue.popleft()
                    if decision.revision_token != self._runtime.current_generation_token(
                        session_id
                    ):
                        self._record_cancelled(session_id, decision, "generation_revision")
                        continue
                    consumed = await self._maybe_speak(session_id, decision)
                    if not consumed:
                        queue.appendleft(decision)
                        await asyncio.sleep(self._cfg.tick_ms / 1000.0)
                        event.set()
                        break
                    await self._fill_prepared(session_id)
        except asyncio.CancelledError:
            return

    async def _maybe_speak(self, session_id: str, decision: Decision) -> bool:
        """Attempt to acquire the lock and run the orchestrator for a decision.

        Builds a FRESH ``StreamOrchestrator`` + ``BoundedVideoQueue`` +
        ``CoordinatorMetrics`` for this call so concurrent sessions do not
        corrupt each other's per-turn state (cancel_event, queue, metrics,
        running_session). Mirrors the per-turn pattern in
        ``core/api/v1.py::_streaming_say``.
        """
        st = self._stats.get(session_id)
        if st is None:
            return True

        # Playback is serialized. A lock may belong to manual speech or an
        # active backend turn, so never release it from a queued decision.
        if self._lock_registry.is_locked(session_id):
            return False

        # 8. Try to acquire the lock.
        ok = self._lock_registry.try_acquire(session_id)
        if not ok:
            return False

        # Stash the decision score for interrupt comparison.
        decision_score = float(decision.score)
        self._active_score[session_id] = decision_score
        self._current_speech[session_id] = decision
        playback_started = time.monotonic()
        decision.latency_spans["playback"] = {
            "start": playback_started,
            "end": playback_started,
        }

        # 9. Prepared scripts use verbatim playback; cloud fallback still lets
        # its full pipeline generate when preparation is intentionally deferred.
        text = decision.prepared_script or decision.prompt or decision.text
        ds = self._runtime._sessions.get(session_id)
        if ds is not None:
            decision.prompt_layers = self._runtime.prompt_layers(session_id, decision)
        else:
            from backend.config import BASE_SALE_PERSONA

            stage_task = decision.prompt or decision.text or ""
            decision.prompt_layers = {
                "base_role": BASE_SALE_PERSONA,
                "shop_profile": "",
                "stage_task": stage_task,
                "final_prompt": (
                    f"SYSTEM ROLE\n{BASE_SALE_PERSONA}\n\n"
                    f"SHOP PROFILE\nChưa cấu hình\n\nSTAGE TASK\n{stage_task}"
                ),
            }
        if not text:
            self._lock_registry.release(session_id)
            self._active_score.pop(session_id, None)
            self._current_speech.pop(session_id, None)
            st.skips += 1
            return True

        # 10. Build a FRESH orchestrator+queue+metrics for this turn and run it.
        # Cloud (FullPipelineBackend) path: backend.say() — no stream_audio
        # (StreamOrchestrator needs streaming avatar, only self-host Stage 3).
        from backend.application.render.engines_base import FullPipelineBackend

        queue = BoundedVideoQueue(max_size=self._max_queue_windows)
        metrics = CoordinatorMetrics()
        orchestrator = StreamOrchestrator(
            llm=self._llm,
            tts=self._tts,
            backend=self._backend,
            queue=queue,
            metrics=metrics,
            fixed_config=self._fixed_config,
            controller_config=self._controller_config,
            audio_window_callback=self._audio_window_callback,
        )
        self._register_speaking(session_id, orchestrator, queue)
        try:
            await self._emit(
                session_id,
                {
                    "type": "coordinator.speak_started",
                    "turn_id": decision.turn_id,
                    "state": "processing",
                    "stage": decision.stage,
                    "task_id": decision.task_id,
                    "action": decision.action,
                    "product": decision.product_id,
                },
            )
            max_attempts = 1 + (ds.director.cfg.transient_retry_count if ds is not None else 0)
            spoken_script = None
            for attempt in range(max_attempts):
                decision.attempt = attempt
                try:
                    generate = decision.prompt is not None and decision.prepared_script is None
                    if isinstance(self._backend, FullPipelineBackend):
                        spoken_script = await asyncio.to_thread(
                            self._backend.say,
                            session_id,
                            text,
                            generate,
                        )
                    elif decision.prepared_script is not None or not generate:
                        spoken_script = await orchestrator.speak_verbatim(session_id, text)
                    else:
                        system_prompt = ds.system_prompt if ds is not None else None
                        spoken_script = await orchestrator.run(
                            session_id,
                            text,
                            system_prompt=system_prompt,
                        )
                    break
                except asyncio.CancelledError:
                    raise
                except (TimeoutError, ConnectionError) as exc:
                    if attempt + 1 >= max_attempts:
                        raise
                    await self._emit(
                        session_id,
                        {
                            "type": "coordinator.retry_scheduled",
                            "turn_id": decision.turn_id,
                            "retry_count": attempt + 1,
                            "error": type(exc).__name__,
                        },
                    )
                    await asyncio.sleep(min(0.1 * (2**attempt), 0.5))
            if decision.is_cancelled or (
                decision.revision_token and not self._runtime.has(session_id)
            ):
                return True
            decision.latency_spans["playback"]["end"] = time.monotonic()
            completed = {
                "turn_id": decision.turn_id,
                "latency_spans": dict(decision.latency_spans),
                "state": "completed",
                "action": decision.action,
                "product_id": decision.product_id,
                "stage": decision.stage,
                "task_id": decision.task_id,
                "script": spoken_script,
                "attempt": decision.attempt,
            }
            self._completed_speech[session_id] = completed
            history = self._completed_history.setdefault(
                session_id, deque(maxlen=self._completed_history_size)
            )
            history.append(completed)
            st.decisions_emitted += 1
            st.last_decision_ts = time.monotonic()
            ds = self._runtime._sessions.get(session_id)
            if ds is not None:
                decision.prepared_script = spoken_script
                decision.completed_at = ds.now()
                ds.director.mark_spoken(decision)
            await self._persist_decision(session_id, decision, text)
            self._after_speak(session_id, decision, text)
            await self._emit(
                session_id,
                {
                    "type": "coordinator.speak_finished",
                    "turn_id": decision.turn_id,
                    "state": "completed",
                    "stage": decision.stage,
                    "task_id": decision.task_id,
                    "action": decision.action,
                    "product_id": decision.product_id,
                },
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception(
                "speech pipeline failed session=%s turn=%s", session_id, decision.turn_id
            )
            failure_state = "playback_timeout" if isinstance(exc, TimeoutError) else "failed"
            await self._emit(
                session_id,
                {
                    "type": "coordinator.speak_failed",
                    "turn_id": decision.turn_id,
                    "state": failure_state,
                    "action": decision.action,
                    "product_id": decision.product_id,
                    "stage": decision.stage,
                    "task_id": decision.task_id,
                    "error": type(exc).__name__,
                    "attempt": decision.attempt,
                },
            )
            failed = {
                **self._speech_item(decision, failure_state),
                "error": type(exc).__name__,
            }
            self._completed_speech[session_id] = failed
            self._completed_history.setdefault(
                session_id, deque(maxlen=self._completed_history_size)
            ).append(failed)
            st.skips += 1
            await self._emit(
                session_id,
                {"type": "coordinator.terminal_failure", "state": failure_state},
            )
            return True
        finally:
            self._lock_registry.release(session_id)
            self._active_score.pop(session_id, None)
            self._current_speech.pop(session_id, None)
            self._unregister_speaking(session_id)

    async def _persist_decision(self, session_id: str, decision: Decision, speech: str) -> None:
        """Delegate persistence to the canonical events module (OpenSpec 1.21)."""
        from .events import persist_decision

        phase = None
        ds = self._runtime._sessions.get(session_id)
        if ds is not None:
            phase = ds.director.state.cursor.phase if ds.director.state.cursor else None
        await persist_decision(
            self._pg_store,
            session_id,
            decision,
            speech,
            phase=phase,
        )

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
                from .scoring import mark_coverage

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
            "sell_product",
            "answer_fact",
            "close",
        ) or (decision.action == "answer_cluster" and not decision.may_interrupt):
            n = len(key_points) if key_points else 1
            state.advance_talking_point(n)
            # Keep cursor phase in sync with FSM phase.
            state.cursor.phase = state.phase.value
            state.cursor.product_idx = state.current_product_index


__all__ = ["DirectorCoordinator", "CoordinatorConfig"]
