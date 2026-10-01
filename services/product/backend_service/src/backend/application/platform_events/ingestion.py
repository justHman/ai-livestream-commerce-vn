"""PlatformEventIngestionService — canonical multi-platform viewer ingress (OpenSpec 2.3-2.7).

Lives below the HTTP transport: the /events route validates the request
(Pydantic) then delegates the whole batch here. The service:

  1. checks the session exists (no meta AND no coordinator session -> 404),
  2. dedups ``event_id`` against a bounded, session-scoped index persisted
     in session meta (survives restarts through the SessionStore boundary),
  3. structurally rejects unusable events (stale timestamp, missing viewer
     id on comment, empty/oversized comment text) with reason codes,
  4. persists accepted/rejected event metadata fire-and-forget via the
     optional pg_store (failures logged, never blocking),
  5. routes ``viewer.comment`` into the coordinator ChatQueue when a
     coordinator session is active. When the caller opts in with
     ``delivery_outcomes_v1`` (P0-FB-013), the ABSENCE of a coordinator is
     retryable ``not_ready`` and nothing is parked — the API owns durable
     retry, so the Runtime never presents a park as success. Without the
     opt-in the legacy park-on-meta + ``accepted`` contract is kept exactly,
     because the deployed API cannot read the new vocabulary; the old sync
     DirectorRuntime.ingest fallback is removed (OpenSpec 2.12),
  6. notifies the FastReducer of every accepted comment — the event-driven
     wakeup for the fast lane (OpenSpec 4.1); duplicate/rejected events
     never notify,
  7. routes join/follow/like to session signals only — never embedded.

Only the coordinator path performs semantic reduction; the service never
branches on ``platform``.

Concurrency: the read -> decide -> write dedup critical section in
``ingest()`` is serialized per session. Redis-backed stores provide a
distributed per-session lock (``with_session_lock`` — Redis ``SET NX``), and
the WHOLE section runs under it, so concurrent ``/events`` for the same
session across processes (rolling deploy ``deployment_maximum_percent=200``,
autoscale, operator) cannot both accept the same ``event_id`` AND concurrent
metadata updates (viewers/signals/pending_platform_chat) are never lost
(P1-04). In-memory stores (single-process by construction) keep the
per-session ``asyncio.Lock`` fallback. A lock-acquisition timeout raises
``SessionLockTimeout`` (the HTTP route maps it to 503); the batch is never
processed unlocked.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import dataclasses
from dataclasses import dataclass
import hashlib
import json
import logging
import time
from enum import Enum
from typing import Any, Callable, Optional

from backend.application.db import SessionStore
from backend.application.db.session_store import SessionLockTimeout, StaleOwnerWriteError
from backend.application.safety_gate import SafetyGate
from backend.application.safety_gate.intake import IntakeSafety

from .models import (
    MAX_STALENESS_SEC,
    P0_COMMENT_CONTRACT,
    CommentPayload,
    PlatformEvent,
)

logger = logging.getLogger(__name__)

_DEDUP_KEY = "platform_event_ids"
_PENDING_KEY = "pending_platform_chat"
_SIGNALS_KEY = "signal_counts"
_VIEWERS_KEY = "unique_viewer_ids"
_BINDING_KEY = "platform_event_binding"


def _provenance(event: PlatformEvent) -> dict[str, Any]:
    return {
        "contract_version": event.contract_version,
        "tenant_id": event.tenant_id,
        "business_session_id": event.business_session_id,
        "platform": event.platform,
        "connected_account_id": event.connected_account_id,
        "external_session_id": event.external_session_id,
        "source_message_id": event.source_message_id,
        "source_stream_id": event.source_stream_id,
        "event_id": event.event_id,
        "viewer_id": event.viewer.viewer_id if event.viewer else None,
        "occurred_at": event.occurred_at,
        "moderation_ref": event.moderation_ref,
    }


class EventStatus(str, Enum):
    """Per-event batch outcome."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    # Truthful delivery outcomes (P0-FB-013). Emitted ONLY when the request
    # opts in via ``delivery_outcomes_v1``; the deployed API does not
    # understand them yet, so the flag and the parking removal below are one
    # activation slice.
    ROUTED = "routed"
    CONSUMED = "consumed"
    NOT_READY = "not_ready"
    NON_DELIVERABLE = "non_deliverable"


# Retryable outcomes must not consume the dedup/action identity: a later
# retry of the same event has to be able to succeed.
_RETRYABLE_OUTCOMES = frozenset({EventStatus.NOT_READY, EventStatus.NON_DELIVERABLE})


@dataclass(frozen=True)
class DeliveryResult:
    """What actually happened to one event, and the identity it would occupy.

    ``action_identity`` is the stable identity a retry must reuse: the
    p0.v1 source hash when one exists, otherwise the event id.
    """

    outcome: EventStatus
    reason: str
    action_identity: str
    comment_id: Optional[str] = None

    @property
    def is_retryable(self) -> bool:
        return self.outcome in _RETRYABLE_OUTCOMES


@dataclass(frozen=True)
class _InFlightDelivery:
    """A routed delivery awaiting consumption by the coordinator.

    ``attach_seq`` is the coordinator's delivery counter when the event was
    routed; a teardown reporting an older counter is stale and must not
    overwrite this entry.
    """

    event: PlatformEvent
    delivery: DeliveryResult
    event_type: str
    attach_seq: int


# Structural rejection precedes the Runtime SafetyGate.
_REASON_STALE = "occurred_at_out_of_range"


def _default_unique_viewer_key(event: PlatformEvent) -> Optional[str]:
    """Stable unique-viewer key: ``{platform}:{source_stream_id}:{viewer_id}``.

    None when the event carries no viewer identity (not attributable).
    """
    if event.viewer is None or not event.viewer.viewer_id:
        return None
    return f"{event.platform}:{event.source_stream_id}:{event.viewer.viewer_id}"


class PlatformEventIngestionService:
    """Session-scoped canonical event ingestion (one instance per app)."""

    def __init__(
        self,
        store: SessionStore,
        pg_store: Any = None,
        coordinator: Any = None,
        runtime: Any = None,
        *,
        max_events_per_request: int = 100,
        dedup_window_sec: float = 3600.0,
        dedup_max_ids: int = 1000,
        unique_viewer_key_fn: Callable[[PlatformEvent], Optional[str]] = _default_unique_viewer_key,
        now_fn: Optional[Callable[[], float]] = None,
        reducer: Any = None,
        lock_acquire_timeout_seconds: float = 2.0,
        safety_gate: SafetyGate | None = None,
    ) -> None:
        self._store = store
        self._pg_store = pg_store
        self._coordinator = coordinator
        self._runtime = runtime
        self._reducer = reducer
        self._safety = IntakeSafety(safety_gate or SafetyGate())
        self._max_events_per_request = max_events_per_request
        self._dedup_window_sec = dedup_window_sec
        self._dedup_max_ids = dedup_max_ids
        self._unique_viewer_key_fn = unique_viewer_key_fn
        self._now = now_fn or time.time
        self._lock_acquire_timeout_seconds = lock_acquire_timeout_seconds
        # Per-session in-process ingress lock: serializes the read -> decide ->
        # write dedup critical section within one process. Redis-backed stores
        # REPLACE this with a distributed SET NX lock in ingest() so sessions
        # coordinate across processes; the memory fallback is single-process by
        # construction (P1-04).
        self._ingest_locks: dict[str, asyncio.Lock] = {}
        # Sanitized rejection counters (no raw viewer content), observable via stats().
        self._rejection_counts: dict[str, int] = {}
        self._accepted_count: int = 0
        # Delivery ledger: routed work awaiting consumption, and the terminal
        # outcome log a teardown reconciles into (P0-FB-013 Task 2).
        self._in_flight: dict[str, dict[str, _InFlightDelivery]] = {}
        self._outcome_log: dict[str, dict[str, _InFlightDelivery]] = {}

    async def _session_exists(self, session_id: str) -> bool:
        """A session exists if it has store meta or a live coordinator session."""
        if self._store is not None and await self._store.exists(session_id):
            return True
        if self._coordinator is not None:
            try:
                if self._coordinator.has(session_id):
                    return True
            except Exception:
                logger.debug("coordinator.has failed session=%s", session_id, exc_info=True)
        return False

    async def _load_meta(self, session_id: str) -> dict:
        if self._store is None:
            return {}
        try:
            return dict(await self._store.get(session_id) or {})
        except Exception:
            logger.warning("session meta read failed session=%s", session_id, exc_info=True)
            raise

    async def _save_meta(
        self, session_id: str, meta: dict, *, fence: Any = None, strict: bool = False
    ) -> None:
        if self._store is None:
            return
        if fence is not None:
            # Fenced (distributed-lock) path: the write is rejected outright if
            # we lost the lock to a newer owner — never swallow a clobber.
            if not await self._store.commit_if_owner(fence, meta):
                raise StaleOwnerWriteError(session_id)
            return
        try:
            await self._store.set(session_id, meta)
        except Exception:
            if strict:
                raise
            logger.warning("session meta write failed session=%s", session_id, exc_info=True)

    async def _screen(self, session_id, meta, text, *, fence=None, defer_replay=False, **context):
        previous_recent = (meta.get("runtime_safety") or {}).get("recent", [])
        evidence = self._safety.evaluate(meta, text, now=self._now(), **context)
        # Record the decision before downstream work. For canonical events the
        # replay observation commits with existing acceptance/dedup state, so
        # a failed routing attempt does not consume a legitimate retry's budget.
        persisted = meta
        if defer_replay and evidence["accepted"]:
            persisted = {
                **meta,
                "runtime_safety": {**meta["runtime_safety"], "recent": previous_recent},
            }
        await self._save_meta(session_id, persisted, fence=fence, strict=True)
        logger.info("runtime.safety_decision session=%s evidence=%s", session_id, evidence)
        if self._pg_store is not None and getattr(self._pg_store, "enabled", False):
            try:
                await self._pg_store.insert_audit_event(
                    "runtime.safety_decision",
                    session_id=session_id,
                    actor="runtime",
                    resource=context.get("event_id") or context["route"],
                    detail=evidence,
                )
            except Exception:
                logger.warning("Safety audit persistence failed session=%s", session_id)
        return evidence

    async def screen_direct_generation(self, session_id: str, text: str) -> dict:
        """Existing active /say generation uses the same local gate/window.

        This is not canonical viewer ingestion or API moderation approval;
        it never supplies a moderation reference or queues a viewer event.
        The approved-speech boundary still controls every output byte.
        """
        async with self._session_lock(session_id) as fence:
            if not await self._session_exists(session_id):
                raise KeyError(session_id)
            meta = await self._load_meta(session_id)
            return await self._screen(session_id, meta, text, fence=fence, route="direct_say")

    # ------------------------------------------------------------------
    # Session signals (join/follow/like) — never embedded
    # ------------------------------------------------------------------

    def _apply_signal(self, meta: dict, event: PlatformEvent) -> None:
        """Update join/follow/like signal counters on the shared meta dict."""
        signals = dict(meta.get(_SIGNALS_KEY) or {})
        key = event.type.removeprefix("viewer.")
        count = getattr(event.payload, "count", None)
        signals[key] = int(signals.get(key, 0)) + int(count or 1)
        meta[_SIGNALS_KEY] = signals

    def _record_viewer_key(self, meta: dict, event: PlatformEvent) -> None:
        """Normalize stable unique-viewer identity into the shared meta dict."""
        viewers = list(meta.get(_VIEWERS_KEY) or [])
        viewer_key = self._unique_viewer_key_fn(event)
        if viewer_key is not None and viewer_key not in viewers:
            viewers.append(viewer_key)
            meta[_VIEWERS_KEY] = viewers[-1000:]

    def _route_comment(
        self,
        meta: dict,
        session_id: str,
        event: PlatformEvent,
        action_identity: str,
        *,
        truthful_outcomes: bool,
    ) -> DeliveryResult:
        """Enqueue a comment into the coordinator queue, or park it on meta.

        With ``truthful_outcomes`` the absence of a coordinator is reported as
        retryable ``not_ready`` and nothing is parked (P0-FB-013) — the API
        owns durable retry, so the Runtime must not present a park as
        success. Without it, the legacy park-and-report-accepted contract is
        preserved exactly for the API that does not understand the vocabulary.
        """
        text = event.payload.text if isinstance(event.payload, CommentPayload) else ""
        author = "viewer"
        if event.viewer is not None:
            author = event.viewer.display_name or event.viewer.viewer_id
        ts = event.occurred_at
        if self._coordinator is not None and self._coordinator.has(session_id):
            if truthful_outcomes and not self._has_queue_capacity(session_id):
                # ChatQueue.put evicts the oldest comment when it is over
                # max_size. Reporting that as routed would be a silent loss
                # wearing a success label, so refuse and keep the identity.
                return DeliveryResult(EventStatus.NOT_READY, "queue_full", action_identity)
            comment = self._coordinator.ingest(session_id, text, author=author, ts=ts)
            outcome = EventStatus.ROUTED if truthful_outcomes else EventStatus.ACCEPTED
            return DeliveryResult(outcome, "coordinator_queued", action_identity, comment.id)
        if truthful_outcomes:
            return DeliveryResult(EventStatus.NOT_READY, "no_coordinator_attached", action_identity)
        pending = list(meta.get(_PENDING_KEY) or [])
        pending.append(
            {
                "event_id": event.event_id,
                "text": text,
                "author": author,
                "ts": ts,
                "platform": event.platform,
                "provenance": _provenance(event),
            }
        )
        meta[_PENDING_KEY] = pending[-100:]
        return DeliveryResult(EventStatus.ACCEPTED, "parked_on_meta", action_identity)

    def _has_queue_capacity(self, session_id: str) -> bool:
        """True when the coordinator's chat queue can take one more comment.

        ``ChatQueue`` silently evicts its oldest entry past ``max_size`` and
        exposes no headroom of its own, so the coordinator publishes
        ``queue_capacity()`` (free slots, 0 when full) and a coordinator
        without it is treated as unbounded rather than assumed full.
        """
        capacity_fn = getattr(self._coordinator, "queue_capacity", None)
        if capacity_fn is None:
            return True
        try:
            return int(capacity_fn(session_id)) > 0
        except Exception:
            logger.warning(
                "coordinator.queue_capacity failed session=%s", session_id, exc_info=True
            )
            return True

    # ------------------------------------------------------------------
    # Persistence (fire-and-forget, never blocks semantic processing)
    # ------------------------------------------------------------------

    async def _persist_accepted(self, session_id: str, event: PlatformEvent) -> None:
        """Persist accepted comment metadata; failures logged and swallowed."""
        if self._pg_store is None or not getattr(self._pg_store, "enabled", False):
            return
        try:
            await self._pg_store.insert_viewer_msg(
                session_id,
                event.payload.text if isinstance(event.payload, CommentPayload) else "",
                author=(
                    event.viewer.display_name or event.viewer.viewer_id
                    if event.viewer is not None
                    else "viewer"
                ),
                comment_id=None,
                source=event.platform,
                payload=_provenance(event),
            )
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_viewer_msg",
                session_id,
            )

    async def _persist_rejected(self, session_id: str, event: PlatformEvent, reason: str) -> None:
        """Audit a rejected event with a sanitized reason (no raw viewer text)."""
        await self._audit(session_id, "event_ingress.rejected", event, reason)

    async def _persist_non_deliverable(self, session_id: str, entry: _InFlightDelivery) -> None:
        """Audit routed work that died with the coordinator (P0-FB-013)."""
        await self._audit(
            session_id,
            "event_ingress.non_deliverable",
            entry.event,
            entry.delivery.reason,
            extra={
                "action_identity": entry.delivery.action_identity,
                "comment_id": entry.delivery.comment_id,
            },
        )

    async def _audit(
        self,
        session_id: str,
        kind: str,
        event: PlatformEvent,
        reason: str,
        *,
        extra: dict | None = None,
    ) -> None:
        if self._pg_store is None or not getattr(self._pg_store, "enabled", False):
            return
        try:
            await self._pg_store.insert_audit_event(
                kind,
                session_id=session_id,
                actor=event.platform,
                resource=f"{event.type}:{event.event_id}",
                detail={
                    "reason": reason,
                    "source_stream_id": event.source_stream_id,
                    **(extra or {}),
                },
            )
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_audit_event",
                session_id,
            )

    # ------------------------------------------------------------------
    # Dedup index (bounded, durable through session meta)
    # ------------------------------------------------------------------

    def _dedup_entries(self, meta: dict) -> list[dict]:
        entries = meta.get(_DEDUP_KEY) or []
        return [entry for entry in entries if isinstance(entry, dict)]

    def _seen_event_ids(self, meta: dict, now: float) -> set[str]:
        cutoff = now - self._dedup_window_sec
        return {
            entry["event_id"] for entry in self._dedup_entries(meta) if entry.get("ts", 0) >= cutoff
        }

    async def _record_seen(
        self,
        session_id: str,
        meta: dict,
        event_id: str,
        *,
        fence: Any = None,
        source_key: str | None = None,
    ) -> None:
        entries = self._dedup_entries(meta)
        now = self._now()
        cutoff = now - self._dedup_window_sec
        entries = [entry for entry in entries if entry.get("ts", 0) >= cutoff]
        entries.append({"event_id": event_id, "ts": now, "source_key": source_key})
        if len(entries) > self._dedup_max_ids:
            entries = entries[-self._dedup_max_ids :]
        meta[_DEDUP_KEY] = entries
        await self._save_meta(session_id, meta, fence=fence)

    # ------------------------------------------------------------------
    # Per-event processing
    # ------------------------------------------------------------------

    def _reject_reason(self, event: PlatformEvent, now: float) -> Optional[str]:
        """Structural pre-embedding rejection before safety evaluation."""
        if abs(now - event.occurred_at) > MAX_STALENESS_SEC:
            return _REASON_STALE
        return None

    def _binding_reject_reason(self, event: PlatformEvent, meta: dict) -> Optional[str]:
        binding = meta.get(_BINDING_KEY)
        if (
            event.contract_version != P0_COMMENT_CONTRACT
            and binding is None
            and not meta.get("execution_contract")
        ):
            return None  # existing non-P0 sessions retain their reader contract
        if event.contract_version != P0_COMMENT_CONTRACT:
            return "p0_contract_required"
        if not isinstance(binding, dict) or binding.get("contract_version") != P0_COMMENT_CONTRACT:
            return "p0_binding_missing"
        for name in (
            "tenant_id",
            "business_session_id",
            "platform",
            "connected_account_id",
            "external_session_id",
        ):
            if getattr(event, name) != binding.get(name):
                return f"p0_{name}_mismatch"
        if event.source_stream_id != binding.get("business_session_id"):
            return "p0_source_stream_id_mismatch"
        return None

    def _notify_reducer(
        self, session_id: str, event: PlatformEvent, comment_id: Optional[str]
    ) -> None:
        """Wake the FastReducer with the accepted comment (OpenSpec 4.1).

        Fires for BOTH accepted routing outcomes — coordinator-queued and
        meta-parked — because both are accepted semantic items (Decision 2).
        The reducer only ever sees accepted comments; SafetyGate runs before
        this path. Imported lazily to avoid a circular import (the reducer
        package never imports platform_events).
        """
        if self._reducer is None:
            return
        from backend.application.reducer import AcceptedComment

        self._reducer.notify_new_events(
            session_id,
            comment=AcceptedComment(
                event_id=event.event_id,
                comment_id=comment_id or event.event_id,
                text=event.payload.text if isinstance(event.payload, CommentPayload) else "",
                ts=event.occurred_at,
                viewer_key=self._unique_viewer_key_fn(event),
                provenance=_provenance(event),
            ),
        )

    async def _process_event(
        self,
        session_id: str,
        event: PlatformEvent,
        meta: dict,
        now: float,
        *,
        fence: Any = None,
        truthful_outcomes: bool = False,
    ) -> dict:
        """Handle one event; returns the per-event result item."""
        result: dict[str, Any] = {"event_id": event.event_id}
        # Validate scope before consulting or mutating dedup/replay state.
        # A forged cross-tenant event must not suppress a legitimate retry.
        binding_reason = self._binding_reject_reason(event, meta)
        if binding_reason is not None:
            await self._persist_rejected(session_id, event, binding_reason)
            return {**result, "status": EventStatus.REJECTED.value, "reason": binding_reason}
        source_key = None
        if event.contract_version == P0_COMMENT_CONTRACT and event.source_message_id:
            source_key = hashlib.sha256(
                json.dumps(
                    [
                        event.tenant_id,
                        event.business_session_id,
                        event.platform,
                        event.connected_account_id,
                        event.external_session_id,
                        event.source_message_id,
                    ]
                ).encode()
            ).hexdigest()
        seen = self._seen_event_ids(meta, now)
        source_seen = source_key is not None and any(
            entry.get("source_key") == source_key
            and entry.get("ts", 0) >= now - self._dedup_window_sec
            for entry in self._dedup_entries(meta)
        )
        if event.event_id in seen or source_seen:
            result["status"] = EventStatus.DUPLICATE.value
            return result

        reason = self._reject_reason(event, now)
        if reason is None and event.type == "viewer.comment":
            evidence = await self._screen(
                session_id,
                meta,
                event.payload.text if isinstance(event.payload, CommentPayload) else "",
                fence=fence,
                route="canonical_events",
                defer_replay=True,
                event_id=event.event_id,
                moderation_ref=event.moderation_ref,
            )
            result["safety"] = evidence
            if not evidence["accepted"]:
                reason = evidence["reason_codes"][0]
        if reason is not None:
            result["status"] = EventStatus.REJECTED.value
            result["reason"] = reason
            self._rejection_counts[reason] = self._rejection_counts.get(reason, 0) + 1
            await self._persist_rejected(session_id, event, reason)
            # Replay-flood is a bounded-window rejection, not permanent
            # delivery ownership. A retry after expiry must be reevaluated.
            if reason != "replay_flood":
                await self._record_seen(
                    session_id, meta, event.event_id, fence=fence, source_key=source_key
                )
            return result

        if event.type == "viewer.comment":
            # Routing decides the outcome; it is never assumed up front.
            delivery = self._route_comment(
                meta,
                session_id,
                event,
                source_key or event.event_id,
                truthful_outcomes=truthful_outcomes,
            )
            result["status"] = delivery.outcome.value
            if truthful_outcomes:
                # New keys ride the opt-in only: the deployed API's response
                # shape is byte-for-byte unchanged while the flag is off.
                result["reason"] = delivery.reason
                result["action_identity"] = delivery.action_identity
            if delivery.comment_id is not None:
                result["comment_id"] = delivery.comment_id
            if delivery.outcome is EventStatus.ROUTED:
                self._track_routed(
                    session_id, event, delivery, seq=self._next_delivery_seq(session_id)
                )
            if delivery.is_retryable:
                # Identity preserved: no dedup record, so a durable retry can
                # re-drive this exact event once a coordinator exists.
                return result
            await self._persist_accepted(session_id, event)
            # The reducer is notified at route time, for both accepted outcomes.
            # A P0 session whose traffic actually opts into truthful outcomes is
            # the one exception: it is marked ready here (the opt-in is now
            # demonstrably live) and fed at CONSUMPTION instead, so a comment
            # that dies with the queue never reaches the reducer. A P0 session
            # still on the legacy contract keeps today's call verbatim.
            if self._reducer_deferral_active(session_id, truthful_outcomes):
                self._mark_reducer_ready(session_id)
            else:
                self._notify_reducer(session_id, event, delivery.comment_id)
        else:
            result["status"] = EventStatus.ACCEPTED.value
            self._apply_signal(meta, event)
        self._record_viewer_key(meta, event)
        await self._record_seen(
            session_id, meta, event.event_id, fence=fence, source_key=source_key
        )
        self._accepted_count += 1
        return result

    # ------------------------------------------------------------------
    # Delivery ledger (P0-FB-013 Task 2)
    #
    # Work handed to the coordinator is not done when it is queued. If the
    # coordinator tears down first, the queue is dropped and the comment dies
    # with it. The ledger remembers what is still in flight so the teardown
    # can be reconciled as audited ``non_deliverable`` instead of being lost.
    # Every entry carries a monotonic sequence, so a reconciliation that
    # predates a newer outcome cannot overwrite it.
    # ------------------------------------------------------------------

    def _next_delivery_seq(self, session_id: str) -> int:
        """Monotonic delivery sequence; 0 means the coordinator has none.

        The session id is REQUIRED here: the teardown fence this stamp is
        compared against is the per-session counter ``stop`` returns, so a
        cross-session total would stamp the entry above its own session's
        fence and it would be skipped forever (P0-FB-013 F3).
        """
        seq_fn = getattr(self._coordinator, "next_delivery_tick", None)
        if seq_fn is None:
            return 0
        try:
            return int(seq_fn(session_id))
        except Exception:
            logger.warning("coordinator.next_delivery_tick failed", exc_info=True)
            return 0

    def _track_routed(
        self, session_id: str, event: PlatformEvent, delivery: DeliveryResult, *, seq: int
    ) -> None:
        """Record a routed comment as still in flight until it is consumed."""
        entries = self._in_flight.setdefault(session_id, {})
        if event.event_id in entries:
            return
        entries[event.event_id] = _InFlightDelivery(
            event=event, delivery=delivery, event_type=event.type, attach_seq=seq
        )

    def mark_consumed(self, session_id: str, comment_ids: set[str]) -> None:
        """Record that the coordinator really consumed these queued comments.

        The consumption boundary is the coordinator's tick, which owns the
        ``ChatQueue`` — the only place a comment is read out of it. It is
        handed this ONE sink rather than this service, so the coordinator
        never imports or holds the ingress ledger and no dependency cycle
        exists. A consumed comment becomes terminal ``consumed`` — kept in
        the outcome log so the audit surface still answers for it. A comment
        that never reaches here stays in flight and a teardown reconciles it
        as audited ``non_deliverable`` (P0-FB-013).

        P0-FB-014: for a reducer-mode session this is ALSO where the reducer is
        notified, not at route time. Only a comment the Director actually
        consumed reaches the reducer, and the ledger entry holds the original
        ``PlatformEvent``, so full task-002 provenance survives the trip.
        """
        if not comment_ids:
            return
        outcomes = self._outcomes(session_id)
        reducer_mode = self._reducer_mode(session_id)
        for entry_id, entry in list(self._in_flight.get(session_id, {}).items()):
            if entry.delivery.comment_id in comment_ids:
                # Only an entry that was ROUTED is in the ledger, and only a
                # routed entry can have deferred its notification (the deferral
                # is armed by the same opt-in that produced ``routed``). A
                # legacy ``accepted`` entry was already notified at route time,
                # so notifying again here would double-count it.
                if reducer_mode and entry.delivery.outcome is EventStatus.ROUTED:
                    self._notify_reducer(session_id, entry.event, entry.delivery.comment_id)
                outcomes[entry_id] = dataclasses.replace(
                    entry,
                    delivery=DeliveryResult(
                        EventStatus.CONSUMED,
                        "coordinator_consumed",
                        entry.delivery.action_identity,
                        entry.delivery.comment_id,
                    ),
                )
                del self._in_flight[session_id][entry_id]

    def _mark_reducer_ready(self, session_id: str) -> None:
        """Tell the coordinator the 013 truthful-outcome opt-in is live here."""
        if self._coordinator is None:
            return
        mark = getattr(self._coordinator, "mark_reducer_ready", None)
        if mark is None:
            return
        try:
            mark(session_id)
        except Exception:
            logger.warning("coordinator.mark_reducer_ready failed", exc_info=True)

    def _reducer_deferral_active(self, session_id: str, truthful_outcomes: bool) -> bool:
        """Whether THIS delivery defers the reducer notification to consumption.

        Both conditions are required: the session's Director must be in reducer
        mode, AND this request must actually be on the truthful-outcome opt-in.
        A P0 session whose caller has not opted in is still on the legacy
        contract, and it keeps the route-time notification.
        """
        return truthful_outcomes and self._reducer_mode(session_id)

    def _reducer_mode(self, session_id: str) -> bool:
        """Whether this session's Director is fed by the bounded reducer.

        Asked of the coordinator, which owns the decision-input mode. The check
        is identity-True, not truthy: a test double or a stub that returns a
        Mock for ANY attribute must not be able to silently move a legacy
        session onto the reducer path. Anything that is not literally ``True``
        means the legacy route-time notification, unchanged.
        """
        if self._coordinator is None:
            return False
        mode_fn = getattr(self._coordinator, "reducer_mode", None)
        if mode_fn is None:
            return False
        try:
            return mode_fn(session_id) is True
        except Exception:
            logger.warning("coordinator.reducer_mode failed", exc_info=True)
            return False

    def terminal_outcomes(self, session_id: str) -> dict[str, DeliveryResult]:
        """Every delivery outcome reached for this session, by event id.

        The audit surface for a caller that has to answer "what happened to
        this event" after the session is gone. In-flight (routed, not yet
        consumed) work is included: its current truthful outcome is ``routed``.
        """
        entries = {**self._in_flight.get(session_id, {}), **self._outcomes(session_id)}
        return {event_id: entry.delivery for event_id, entry in entries.items()}

    def _outcomes(self, session_id: str) -> dict[str, _InFlightDelivery]:
        return self._outcome_log.setdefault(session_id, {})

    async def reconcile_session(self, session_id: str, *, attach_seq: int) -> list[str]:
        """Reconcile work lost to a teardown as audited ``non_deliverable``.

        Call once the coordinator for ``session_id`` has stopped consuming.
        Every still-in-flight delivery becomes a terminal
        ``non_deliverable`` and is written to the audit store, so no routed
        comment is dropped without a record.

        ``attach_seq`` is the coordinator's delivery counter at teardown. A
        delivery stamped with a NEWER sequence was routed after this teardown
        and is left alone: a stale report must never overwrite a newer outcome.
        Returns the event ids that were reconciled.
        """
        reconciled: list[str] = []
        for event_id, entry in list(self._in_flight.get(session_id, {}).items()):
            if entry.attach_seq > attach_seq:
                continue
            entry = dataclasses.replace(
                entry,
                delivery=DeliveryResult(
                    EventStatus.NON_DELIVERABLE,
                    "coordinator_torn_down_before_consumption",
                    entry.delivery.action_identity,
                    entry.delivery.comment_id,
                ),
            )
            self._outcomes(session_id)[event_id] = entry
            del self._in_flight[session_id][event_id]
            await self._persist_non_deliverable(session_id, entry)
            reconciled.append(event_id)
        return reconciled

    async def ingest(
        self, session_id: str, events: list[PlatformEvent], *, delivery_outcomes_v1: bool = False
    ) -> dict:
        """Process a bounded batch; returns the per-event result list + counts.

        ``delivery_outcomes_v1`` is the P0-FB-013 activation opt-in: the
        caller declares it can interpret ``routed`` / ``not_ready`` /
        ``non_deliverable``. It is OFF by default so the deployed API, which
        only knows accepted/duplicate/rejected, keeps today's contract
        verbatim; when it is ON, parking is gone and a comment with no
        coordinator is retryable ``not_ready`` rather than a false success.

        Raises KeyError when the session is unknown (no meta, no coordinator).
        Raises SessionLockTimeout when a Redis-backed store's distributed
        per-session lock cannot be acquired in time — the batch is never
        processed unlocked (route maps to 503).

        The read -> decide -> write critical section is serialized per session
        (P1-04): Redis-backed stores coordinate it across processes via a
        ``SET NX`` distributed lock (``with_session_lock``); the in-process
        ``asyncio.Lock`` remains only for single-process memory stores.
        """
        async with self._session_lock(session_id) as fence:
            return await self._ingest_locked(
                session_id, events, fence=fence, truthful_outcomes=delivery_outcomes_v1
            )

    @asynccontextmanager
    async def _session_lock(self, session_id):
        """Canonical and direct input share the existing session serialization."""
        distributed = getattr(self._store, "with_session_lock", None)
        if distributed is not None:
            try:
                async with distributed(
                    session_id, acquire_timeout_seconds=self._lock_acquire_timeout_seconds
                ) as fence:
                    yield fence
                return
            except StaleOwnerWriteError as exc:
                # Lost ownership mid-section == session busy; the route already
                # maps SessionLockTimeout to 503, so the client retries.
                raise SessionLockTimeout(session_id) from exc
        lock = self._ingest_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            yield None

    async def _ingest_locked(
        self,
        session_id: str,
        events: list[PlatformEvent],
        *,
        fence: Any = None,
        truthful_outcomes: bool = False,
    ) -> dict:
        """The locked critical section: existence check -> load -> decide -> write."""
        if not await self._session_exists(session_id):
            raise KeyError(session_id)
        meta = await self._load_meta(session_id)
        now = self._now()
        results = [
            await self._process_event(
                session_id, event, meta, now, fence=fence, truthful_outcomes=truthful_outcomes
            )
            for event in events
        ]
        # The legacy three keys are always present: existing readers index
        # them directly and expect 0, never a missing key.
        counts = {"accepted": 0, "duplicate": 0, "rejected": 0}
        for item in results:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        return {"events": results, **counts}

    def stats(self, session_id: str) -> dict:
        """Content-safe per-session observability for event ingress.

        Counters are per-process (sanitized rejection reasons only); session
        signals/dedup state live in session meta and are read live.
        """
        return {
            "session_id": session_id,
            "accepted": self._accepted_count,
            "rejected_by_reason": dict(self._rejection_counts),
        }
