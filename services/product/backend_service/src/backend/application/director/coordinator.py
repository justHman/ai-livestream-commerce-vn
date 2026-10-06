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
from .errors import CoordinatorUnavailable
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
from ..script_authoring.approved_speech import SpeechRejected

if TYPE_CHECKING:
    # The hub is only used through its async ``emit(session_id, event)``
    # method, so the import stays lazy (TYPE_CHECKING) to avoid cycles.
    from backend.api.v1.hub import ControlHub
    from backend.application.runtime_failures import RuntimeFailures

logger = logging.getLogger(__name__)

# A rejected `close` (no approved closing at P0) leaves the Director in CLOSING with nothing
# spoken, so every tick would re-issue the same close decision. After a rejection, suppress
# `close` for this long, doubling per consecutive rejection.
CLOSE_REJECT_BACKOFF_SEC = 5.0
CLOSE_REJECT_BACKOFF_MAX_SEC = 60.0


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
        reducer: Any = None,
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
        # Per-session count of comments routed into the queue. Fences teardown
        # reconciliation against stale reports (P0-FB-013).
        self._delivery_seq: dict[str, int] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._playback_tasks: dict[str, asyncio.Task] = {}
        self._prepare_tasks: dict[str, set[asyncio.Task]] = {}
        self._decision_locks: dict[str, asyncio.Lock] = {}
        self._playback_events: dict[str, asyncio.Event] = {}
        self._stats: dict[str, _SessionStats] = {}
        self._active_score: dict[str, float] = {}
        self._last_tick: dict[str, float] = {}
        self._resume_count: dict[str, int] = {}
        self._decision_queue: dict[str, deque[Decision]] = {}
        self._speech_queue: dict[str, deque[Decision]] = {}
        self._current_speech: dict[str, Decision] = {}
        self._completed_speech: dict[str, dict] = {}
        self._completed_history: dict[str, deque[dict]] = {}
        self._completed_history_size = completed_history_size
        self._activated: set[str] = set()
        self._autonomous_openings: dict[str, str] = {}
        # session -> (consecutive rejected closes, monotonic not-before)
        self._close_backoff: dict[str, tuple[int, float]] = {}
        self._monotonic = time.monotonic
        self._opening_media: dict[str, dict] = {}
        # Optional Postgres runtime store (durable rows). None/disabled -> no
        # persistence. Fire-and-forget: a failure must never break the speak loop.
        self._pg_store = pg_store
        self._audio_window_callback = audio_window_callback
        # Sink for "these queued comments were really read out of the queue".
        # The app composition root points it at
        # PlatformEventIngestionService.mark_consumed so a comment the tick
        # consumed is not reconciled as lost at teardown (P0-FB-013). One
        # unbound callable keeps the ingress ledger out of this module.
        self.comment_consumed = None
        # The app composition root installs the same boundary used by /say.
        self.approved_speech = None
        self.runtime_failures: RuntimeFailures | None = None  # wired by default-off 020 lifespan
        # The bounded FastReducer (P0-FB-014). One instance serves all sessions;
        # it is consulted ONLY for sessions in reducer mode, so a legacy session
        # is byte-for-byte unchanged.
        self._reducer = reducer
        # Per-session decision-input mode. True = the reducer is the only
        # viewer-demand input; False/absent = the legacy raw-comment feed.
        # Runtime engineering selector, NOT a Product Rule.
        self._reducer_mode: set[str] = set()
        # Sessions whose 013 truthful-outcome opt-in actually fired. Reducer
        # mode refuses to decide for a session that is not in here.
        self._reducer_ready: set[str] = set()
        # Bounded per-session consumed-id marker, used ONLY in reducer mode
        # (the legacy path marks consumption via state.embeddings_cache).
        self._reducer_consumed: dict[str, dict[str, float]] = {}

    # ------------------------------------------------------------------
    # Decision-input mode (P0-FB-014)
    # ------------------------------------------------------------------

    @property
    def reducer(self):
        return self._reducer

    @reducer.setter
    def reducer(self, value) -> None:
        self._reducer = value

    def set_reducer_mode(self, session_id: str, enabled: bool = True) -> None:
        """Select the decision input for one session.

        Reducer mode requires the 013 truthful-outcome opt-in to be ACTIVE for
        that session. ``mark_reducer_ready`` records that opt-in at consumption
        time; until then ``_reducer_store`` returns None and the session
        decides nothing — a refusal, never a silent fallback to the legacy feed.
        """
        if enabled:
            self._reducer_mode.add(session_id)
        else:
            self._reducer_mode.discard(session_id)

    def reducer_mode(self, session_id: str) -> bool:
        return session_id in self._reducer_mode

    def mark_reducer_ready(self, session_id: str) -> None:
        """Record that the 013 truthful-outcome opt-in is live for this session.

        Called from the consumption boundary, which is the only place that can
        observe the opt-in actually firing. Reducer mode refuses until this.
        """
        self._reducer_ready.add(session_id)

    def _reducer_store(self, session_id: str):
        """The session's ClusterStore, or None when reducer mode may not decide."""
        if session_id not in self._reducer_mode or self._reducer is None:
            return None
        if session_id not in self._reducer_ready:
            return None
        return self._reducer.session_store(session_id)

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
        # A NEW generation starts from an empty reducer session (P0-FB-014).
        # ``stop`` already drops it, but a start on a never-stopped id (or a
        # reattach after a crash) must not inherit the previous generation's
        # clusters, answered state or provenance either.
        if self._reducer is not None:
            self._reducer.drop_session(session_id)
        self._reducer_consumed.pop(session_id, None)
        self._reducer_ready.discard(session_id)
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

    def stop(self, session_id: str) -> int:
        """Cancel the tick task, drop the queue, detach runtime.

        If the orchestrator is currently speaking for this session, cancel it.
        Idempotent.

        Returns the delivery counter as of teardown. The caller passes it to
        ``PlatformEventIngestionService.reconcile_session(session_id,
        attach_seq=...)`` so comments that were routed but never consumed are
        reconciled as audited non_deliverable instead of dying with the queue
        (P0-FB-013). The counter is captured BEFORE the state is dropped.
        """
        if self.approved_speech is not None:
            self.approved_speech.cancel(session_id)
            self.approved_speech.block(session_id, None)
        attach_seq = self._delivery_seq.get(session_id, 0)
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
        self._delivery_seq.pop(session_id, None)
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
        self._resume_count.pop(session_id, None)
        self._activated.discard(session_id)
        self._autonomous_openings.pop(session_id, None)
        self._close_backoff.pop(session_id, None)
        self._opening_media.pop(session_id, None)
        self._runtime.detach(session_id)
        self._lock_registry.drop(session_id)
        # Drop the reducer's per-session state too (P0-FB-014): clusters,
        # answered state and provenance must not survive into a new generation
        # of the same session id.
        if self._reducer is not None:
            self._reducer.drop_session(session_id)
        self._reducer_mode.discard(session_id)
        self._reducer_ready.discard(session_id)
        self._reducer_consumed.pop(session_id, None)
        return attach_seq

    def stop_all(self) -> dict[str, int]:
        """Cancel every active coordinator session.

        Returns each session's delivery fence as of its teardown, the same
        value ``stop`` hands back. A caller that bulk-stops (process
        shutdown) needs it for every session, otherwise routed work dies
        with the queues and is never audited (P0-FB-013).
        """
        return {session_id: self.stop(session_id) for session_id in list(self._tasks)}

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

    def _note_rejected(self, session_id: str, decision: Decision) -> None:
        """Back off after a rejected `close` so the Director does not re-issue it every tick."""
        if decision.action != "close":
            return
        count = self._close_backoff.get(session_id, (0, 0.0))[0] + 1
        delay = min(CLOSE_REJECT_BACKOFF_SEC * 2 ** (count - 1), CLOSE_REJECT_BACKOFF_MAX_SEC)
        self._close_backoff[session_id] = (count, self._monotonic() + delay)

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
        if self.approved_speech is not None:
            self.approved_speech.cancel(session_id)
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

    # -- P0-FB-016 Hold / closing start fence ---------------------------------
    def _frozen(self, session_id: str) -> bool:
        """Held, closing or ending: ingest continues, no new turn starts."""
        return self.approved_speech is not None and bool(self.approved_speech.blocked(session_id))

    def _hard_expired(self, session_id: str, decision: Decision) -> bool:
        """Q&A older than the hard-expiry horizon by its original comment time."""
        ds = self._runtime._sessions.get(session_id)
        if ds is None or decision.action not in ("answer_cluster", "answer_fact"):
            return False
        # ponytail: the Director selection window is the hard-expiry horizon
        # (exact windows REQUIRES_VALIDATION); swap in the validated value.
        seen = {c.id: c.t for c in ds.director.state.rolling_comments}
        times = [seen[i] for i in decision.cluster_member_ids if i in seen]
        # Unknown (pruned) members are older than the window: fail closed.
        return not times or ds.now() - max(times) > ds.director.cfg.selection_window_sec

    def resume(self, session_id: str) -> list[str]:
        """Re-check queued Q&A age before anything plays; called after unblock.

        Hard expiry is measured from each member comment's original time
        (BR-QA-002: delay does not reset age). Envelope/content revalidation
        runs again at ``_maybe_speak`` before any queued turn starts.
        """
        expired = []
        self._resume_count[session_id] = self._resume_count.get(session_id, 0) + 1
        ds = self._runtime._sessions.get(session_id)
        if ds is not None:
            for queue in (self._decision_queue.get(session_id), self._speech_queue.get(session_id)):
                if queue is None:
                    continue
                for decision in list(queue):
                    if self._hard_expired(session_id, decision):
                        queue.remove(decision)
                        self._record_cancelled(session_id, decision, "hard_expired")
                        expired.append(decision.turn_id)
            self._last_tick[session_id] = ds.now()
        event = self._playback_events.get(session_id)
        if event is not None:
            event.set()
        return expired

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
            raise CoordinatorUnavailable(f"No active coordinator session: {session_id}")
        self._delivery_seq[session_id] = self._delivery_seq.get(session_id, 0) + 1
        comment = queue.put(text, author, ts=ts)
        # Approved P0 sessions require the authorized execution start command.
        if self._runtime.get_session(session_id).approved_envelope is None:
            self._activated.add(session_id)
        return comment

    def activate_approved(self, session_id: str, opening_turn_id: str, envelope) -> None:
        """Queue exactly one locked opening; called under the execution lock."""
        if session_id in self._autonomous_openings:
            return
        ds = self._runtime.get_session(session_id)
        if not self.has(session_id) or ds.approved_envelope != envelope:
            raise SpeechRejected("session_not_attached")
        product = envelope.products[0]
        decision = Decision(
            action="autonomous_opening",
            stage="opening",
            task_id="approved-opening",
            turn_id=opening_turn_id,
            product_id=product.product_id,
            prepared_script=product.spoken_text,
            revision_token=self._runtime.current_generation_token(session_id),
        )
        self._autonomous_openings[session_id] = opening_turn_id
        self._activated.add(session_id)
        self._decision_queue[session_id].append(decision)
        task = asyncio.create_task(self._prepare_turn(session_id, decision))
        self._prepare_tasks[session_id].add(task)
        task.add_done_callback(
            lambda done: self._prepare_tasks.get(session_id, set()).discard(done)
        )

    def opening_media(self, session_id: str) -> dict | None:
        return self._opening_media.get(session_id)

    async def _record_opening_media(
        self, session_id: str, decision: Decision, utterance_id: str, speech
    ) -> None:
        if (
            self._autonomous_openings.get(session_id) == decision.turn_id
            and self._speech_live(session_id, decision)
            and speech is not None
        ):
            receipt = {
                "opening_turn_id": decision.turn_id,
                "media_utterance_id": utterance_id,
                "approved_envelope_hash": speech.envelope.fingerprint,
            }
            if session_id not in self._opening_media:
                self._opening_media[session_id] = receipt
                await self._emit(session_id, {"type": "execution.opening_media", **receipt})

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

    def queue_capacity(self, session_id: str) -> int:
        """Free slots left in the session's ChatQueue; 0 when full.

        ``ChatQueue.put`` evicts the oldest comment once it exceeds
        ``max_size``, so a producer must be able to ask for headroom BEFORE
        putting or it silently drops someone else's comment (P0-FB-013).
        An unknown session is 0 — there is nowhere to put the comment.
        """
        queue = self._queues.get(session_id)
        if queue is None:
            return 0
        return queue.free_slots()

    def next_delivery_tick(self, session_id: str | None = None) -> int:
        """Monotonic count of comments routed through the session's queue.

        Read by the ingress service to fence teardown reconciliation: a
        delivery stamped with a counter above the teardown's was routed
        after it and must not be reconciled (P0-FB-013).
        """
        if session_id is None:
            return sum(self._delivery_seq.values())
        return self._delivery_seq.get(session_id, 0)

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
                    if self.runtime_failures is not None:
                        self.runtime_failures.tick(session_id, None)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    if self.runtime_failures is not None:
                        logger.error(
                            "coordinator tick error session=%s class=%s",
                            session_id,
                            type(exc).__name__,
                        )
                        self.runtime_failures.tick(session_id, exc)
                    else:
                        logger.exception(
                            "coordinator tick error for session %s (continuing)", session_id
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
        fresh = queue.drain_window(self._cfg.window_sec)
        if session_id in self._reducer_mode:
            # Reducer mode (P0-FB-014): the bounded reducer is the only
            # viewer-demand input. The queue is still drained and the
            # consumption boundary still fires (I-4) — but this coordinator
            # does NOT embed, route, or add to rolling_comments, so the raw
            # decision feed is off for this session. The reducer, fed at
            # consumption by the ingress service, owns the clustering.
            #
            # Nothing is added to embeddings_cache here, so the per-session
            # consumed set is what makes a comment consumed exactly once.
            new_only = [
                comment for comment in fresh if comment.id not in self._consumed_ids(session_id)
            ]
            # Release ChatQueue capacity exactly as the legacy path does (013).
            queue.mark_consumed(c.id for c in new_only)
            if self.comment_consumed is not None and new_only:
                try:
                    self.comment_consumed(session_id, {c.id for c in new_only})
                except Exception:
                    logger.warning(
                        "consumed-comment sink failed session=%s", session_id, exc_info=True
                    )
            self._remember_consumed(session_id, new_only)
            now = ds.now()
            if self._frozen(session_id):
                # Hold freezes Director timers and scheduling, not ingestion.
                self._last_tick[session_id] = now
                return
            self._advance_timers(session_id, now, state)
            await self._fill_prepared(session_id)
            return
        embedder = self._get_embedder()
        new_only = [comment for comment in fresh if comment.id not in state.embeddings_cache]
        new_comments = new_only
        if new_comments:
            vecs = await asyncio.to_thread(
                embedder.encode, [comment.text for comment in new_comments]
            )
            for comment, vector in zip(new_comments, vecs):
                state.embeddings_cache[comment.id] = list(vector)
        director_now = ds.now()
        wall_now = time.time()
        routed = []
        current = state.current_product()
        for incoming in new_comments:
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
        queue.mark_consumed(c.id for c in new_only)
        # This is the consumption boundary: the comments left ChatQueue and
        # are now Director state, so a teardown must not reconcile them as
        # non_deliverable (P0-FB-013). Only ``new_only`` counts — a comment
        # re-read from the window on a later tick was already consumed.
        if self.comment_consumed is not None and new_only:
            try:
                self.comment_consumed(session_id, {c.id for c in new_only})
            except Exception:
                logger.warning("consumed-comment sink failed session=%s", session_id, exc_info=True)
        # Bound the old comment/embedding history at write time (5.10): the
        # ClusterStore owns the long-term demand — this state only feeds the
        # Director's selection window.
        state.prune_history(director_now, director.cfg.selection_window_sec)
        now = ds.now()
        if self._frozen(session_id):
            # Hold freezes Director timers and scheduling, not ingestion.
            self._last_tick[session_id] = now
            return
        self._advance_timers(session_id, now, state)
        await self._fill_prepared(session_id)

    def _consumed_ids(self, session_id: str):
        """Comment ids already consumed in reducer mode.

        In reducer mode nothing is added to ``state.embeddings_cache``, so that
        cache cannot double as the consumed marker. A bounded per-session set
        does: a comment is consumed exactly once (I-4), and a re-read from the
        window on a later tick is not consumed again.
        """
        return self._reducer_consumed.get(session_id, {})

    def _remember_consumed(self, session_id: str, comments) -> None:
        seen = self._reducer_consumed.setdefault(session_id, {})
        for comment in comments:
            seen[comment.id] = comment.ts
        # A comment older than the drain window can never be re-read, so its
        # marker is dead weight: prune on the same horizon the queue uses.
        cutoff = time.time() - self._cfg.window_sec
        for stale in [cid for cid, ts in seen.items() if ts < cutoff]:
            del seen[stale]

    def _decide_from_reducer(self, projection: Director, session_id: str, store, now: float):
        """Decide from the bounded reducer.

        An empty projection still goes through ``decide_from_reducer`` so the
        protected opening, introduction, proactive selling, pivot and
        checkpoint logic run exactly as in legacy. Without a ready store the caller
        decides on an empty rolling window, which agrees with this path.
        """
        from .reducer_input import build_selections

        selections = build_selections(
            projection,
            store=store,
            reducer_now=time.time(),
            director_now=now,
            wall_now=time.time(),
            provenance=(
                None
                if self._reducer is None
                else lambda cid: self._reducer.provenance_for(session_id, cid)
            ),
        )
        high_value_ids = projection.high_value_cluster_ids(selections)
        return projection.decide_from_reducer(
            selections, now, high_value_ids=high_value_ids.__contains__
        )

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
        if ds is None or self._frozen(session_id):
            return
        # Finish the sole approved opening before projecting selling turns.
        if (
            session_id in self._autonomous_openings
            and not ds.director.state.cursor.opening_completed
        ):
            return
        async with self._decision_locks[session_id]:
            # A Hold can land while waiting for the lock: prepare nothing.
            if self._frozen(session_id):
                return
            depth = ds.director.cfg.prepared_turn_depth
            prepared = self._speech_queue[session_id]
            in_preparation = len(self._prepare_tasks[session_id])
            missing = max(0, depth - len(prepared) - in_preparation)
            if missing == 0:
                return
            projection = self._projected_director(session_id)
            store = self._reducer_store(session_id)
            for _ in range(missing):
                if self._frozen(session_id):  # Hold landed during an earlier emit await
                    return
                now = ds.now()
                if store is not None:
                    decision = self._decide_from_reducer(projection, session_id, store, now)
                else:
                    # Reducer mode not ready (or legacy): rolling_comments is
                    # empty in reducer mode, so this is the same empty-demand
                    # _decide the reducer branch runs - never a raw-comment feed.
                    decision = projection.decide(projection.state.rolling_comments, now=now)
                self._stats[session_id].director_cycles += 1
                if decision.action in ("idle", "skip"):
                    self._stats[session_id].skips += 1
                    break
                if (
                    decision.action == "close"
                    and self._monotonic() < self._close_backoff.get(session_id, (0, 0.0))[1]
                ):
                    self._stats[session_id].skips += 1  # a recent close was rejected
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
            if self.approved_speech is not None:
                speech = await self._prepare_approved(session_id, decision)
                decision.approved_speech = speech
                decision.prepared_script = speech.text
                decision.prepared_variants = (speech.text,)
            elif decision.prompt is not None and can_prepare:
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
        except SpeechRejected as exc:
            self._note_rejected(session_id, decision)
            self._record_cancelled(session_id, decision, exc.code)
            await self._emit(
                session_id,
                {
                    "type": "speech.content_rejected",
                    "turn_id": decision.turn_id,
                    "reason": exc.code,
                },
            )
        except Exception as exc:
            if self.runtime_failures is not None:
                current = self._runtime._sessions.get(session_id)
                if current is None or (
                    decision.revision_token and current.generation_token != decision.revision_token
                ):
                    self._record_cancelled(session_id, decision, "generation_revision")
                    return
                self.runtime_failures.fail(session_id, exc, decision.revision_token)
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
            if self.runtime_failures is not None:
                logger.error(
                    "turn preparation failed session=%s class=%s", session_id, type(exc).__name__
                )
            else:
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
                while queue and not self._frozen(session_id):
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

    def _speech_live(self, session_id: str, decision: Decision) -> bool:
        return (
            not decision.is_cancelled
            and self._runtime.has(session_id)
            and decision.revision_token == self._runtime.current_generation_token(session_id)
        )

    async def _prepare_approved(self, session_id: str, decision: Decision):
        return await self.approved_speech.prepare(
            session_id,
            decision.prepared_script or decision.prompt or decision.text or "",
            llm=self._llm,
            generate=decision.prompt is not None and decision.prepared_script is None,
            product_id=decision.product_id,
            select_locked=(
                decision.prepared_script is None
                and decision.action
                in (
                    "introduce_product",
                    "sell_product",
                )
            ),
            route="director",
            live=lambda: self._speech_live(session_id, decision),
        )

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

        speech = None
        resumed_at_entry = self._resume_count.get(session_id, 0)

        def live():
            return self._speech_live(session_id, decision)

        if self.approved_speech is not None:
            try:
                speech = decision.approved_speech
                if speech is None:
                    speech = await self._prepare_approved(session_id, decision)
                    decision.prepared_script = speech.text
                    decision.approved_speech = speech
                if decision.prepared_script != speech.text:
                    raise SpeechRejected("altered_prepared_text")
                await self.approved_speech.revalidate(speech, live=live)
            except SpeechRejected as exc:
                st.skips += 1
                self._note_rejected(session_id, decision)
                self._record_cancelled(session_id, decision, exc.code)
                await self._emit(
                    session_id,
                    {
                        "type": "speech.content_rejected",
                        "turn_id": decision.turn_id,
                        "reason": exc.code,
                    },
                )
                return True

        # Safe speech boundary (P0-FB-016): a held turn stays queued. No await
        # separates this check from lock acquisition below.
        if self._frozen(session_id):
            return False
        # A Hold->Resume during the revalidation await left this popped turn in
        # neither queue, so resume() could not expire it: re-check here.
        if self._resume_count.get(session_id, 0) != resumed_at_entry and self._hard_expired(
            session_id, decision
        ):
            st.skips += 1
            self._record_cancelled(session_id, decision, "hard_expired")
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

        async def opening_audio(window):
            await self._record_opening_media(session_id, decision, window.utterance_id, speech)
            if self._audio_window_callback is not None:
                await self._audio_window_callback(window)

        orchestrator = StreamOrchestrator(
            llm=self._llm,
            tts=self.approved_speech.guarded_tts(speech, self._tts, live=live, emit=self._emit)
            if speech is not None
            else self._tts,
            backend=self._backend,
            queue=queue,
            metrics=metrics,
            fixed_config=self._fixed_config,
            controller_config=self._controller_config,
            audio_window_callback=(
                self.approved_speech.guarded_audio(speech, live=live, callback=opening_audio)
                if speech is not None
                else self._audio_window_callback
            ),
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
            if decision.action == "autonomous_opening":
                # A timed-out provider may already have emitted media.
                max_attempts = 1
            spoken_script = None
            for attempt in range(max_attempts):
                decision.attempt = attempt
                try:
                    generate = decision.prompt is not None and decision.prepared_script is None
                    if speech is not None:
                        await self.approved_speech.revalidate(speech, live=live)
                        await self._emit(
                            session_id,
                            {
                                "type": "speech.content_validated",
                                "turn_id": decision.turn_id,
                                **speech.evidence(),
                            },
                        )
                        self.approved_speech.check_live(speech, live)
                    if isinstance(self._backend, FullPipelineBackend):

                        def cloud_say():
                            if speech is not None:
                                self.approved_speech.check_live(speech, live)
                            return self._backend.say(session_id, text, generate)

                        spoken_script = await asyncio.to_thread(
                            cloud_say,
                        )
                        # Provider completion is a correlation receipt, not viewer-live.
                        await self._record_opening_media(
                            session_id, decision, decision.turn_id, speech
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
                "validation": speech.evidence() if speech is not None else None,
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
            if self.runtime_failures is not None:
                current = self._runtime._sessions.get(session_id)
                if isinstance(exc, SpeechRejected):
                    st.skips += 1
                    self._note_rejected(session_id, decision)
                    self._record_cancelled(session_id, decision, exc.code)
                    await self._emit(
                        session_id,
                        {
                            "type": "speech.content_rejected",
                            "turn_id": decision.turn_id,
                            "reason": exc.code,
                        },
                    )
                    return True
                if current is None or (
                    decision.revision_token and current.generation_token != decision.revision_token
                ):
                    self._record_cancelled(session_id, decision, "generation_revision")
                    return True
                self.runtime_failures.fail(session_id, exc, decision.revision_token)
                logger.error(
                    "speech pipeline failed session=%s turn=%s class=%s",
                    session_id,
                    decision.turn_id,
                    type(exc).__name__,
                )
            else:
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

    def _mark_reducer_lifecycle(self, session_id: str, decision: Decision, now: float) -> None:
        """Reuse the store's own lifecycle state after a reducer-driven speech.

        ``mark_answered`` is what makes the cooldown/novelty state durable
        across ticks, and ``increment_skip`` mirrors the eviction budget the
        legacy path applies to clusters it passed over. Both are no-ops for a
        cluster the bound already evicted.
        """
        if self._reducer is None or decision.source_cluster_id is None:
            return
        store = self._reducer.session_store(session_id)
        if store is None:
            return
        if decision.action in ("answer_fact", "answer_cluster"):
            store.mark_answered(decision.source_cluster_id, now)
        else:
            store.mark_selected(decision.source_cluster_id, now)

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
        self._mark_reducer_lifecycle(session_id, decision, time.time())
        if decision.action == "close":
            self._close_backoff.pop(session_id, None)
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
