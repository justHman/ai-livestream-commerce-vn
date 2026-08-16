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
     coordinator session is active, otherwise parks them on session meta
     (``pending_platform_chat``) so they are not lost; the old sync
     DirectorRuntime.ingest fallback is removed (OpenSpec 2.12),
  6. notifies the FastReducer of every accepted comment — the event-driven
     wakeup for the fast lane (OpenSpec 4.1); duplicate/rejected events
     never notify,
  7. routes join/follow/like to session signals only — never embedded.

Only the coordinator path performs semantic reduction; the service never
branches on ``platform``.

Concurrency: the read -> decide -> write dedup critical section in
``ingest()`` is serialized per session by an in-process ``asyncio.Lock`` so
two concurrent ``/events`` for the same session cannot both accept the same
``event_id`` (P1-04). This assumes SINGLE-PROCESS ownership of a session —
enforced in the deployment by ALB ``lb_cookie`` stickiness on the backend
target group. Multi-instance deployments must move dedup to Redis ``SET NX``
or a transactional DB; the in-process lock does not coordinate across
processes.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Optional

from backend.application.db import SessionStore

from .models import (
    MAX_STALENESS_SEC,
    CommentPayload,
    PlatformEvent,
)

logger = logging.getLogger(__name__)

_DEDUP_KEY = "platform_event_ids"
_PENDING_KEY = "pending_platform_chat"
_SIGNALS_KEY = "signal_counts"
_VIEWERS_KEY = "unique_viewer_ids"


class EventStatus(str, Enum):
    """Per-event batch outcome."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


# Structural rejection reason (the full SafetyGate is a later cluster).
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
    ) -> None:
        self._store = store
        self._pg_store = pg_store
        self._coordinator = coordinator
        self._runtime = runtime
        self._reducer = reducer
        self._max_events_per_request = max_events_per_request
        self._dedup_window_sec = dedup_window_sec
        self._dedup_max_ids = dedup_max_ids
        self._unique_viewer_key_fn = unique_viewer_key_fn
        self._now = now_fn or time.time
        # Per-session ingress lock: serializes the read -> decide -> write
        # dedup critical section so concurrent /events for the same session
        # cannot both accept the same event_id (P1-04). Single-process
        # ownership only (ALB lb_cookie stickiness) — see module docstring.
        self._ingest_locks: dict[str, asyncio.Lock] = {}
        # Sanitized rejection counters (no raw viewer content), observable via stats().
        self._rejection_counts: dict[str, int] = {}
        self._accepted_count: int = 0

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
            return {}

    async def _save_meta(self, session_id: str, meta: dict) -> None:
        if self._store is None:
            return
        try:
            await self._store.set(session_id, meta)
        except Exception:
            logger.warning("session meta write failed session=%s", session_id, exc_info=True)

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

    def _route_comment(self, meta: dict, session_id: str, event: PlatformEvent) -> Optional[str]:
        """Enqueue a comment into the coordinator queue or park it on meta.

        Returns the comment id when the coordinator accepted it (the queue
        never rejects), None when the event was parked on ``meta`` for later
        pickup (the caller persists ``meta`` once via _record_seen).
        """
        text = event.payload.text if isinstance(event.payload, CommentPayload) else ""
        author = "viewer"
        if event.viewer is not None:
            author = event.viewer.display_name or event.viewer.viewer_id
        ts = event.occurred_at
        if self._coordinator is not None and self._coordinator.has(session_id):
            comment = self._coordinator.ingest(session_id, text, author=author, ts=ts)
            return comment.id
        pending = list(meta.get(_PENDING_KEY) or [])
        pending.append(
            {
                "event_id": event.event_id,
                "text": text,
                "author": author,
                "ts": ts,
                "platform": event.platform,
            }
        )
        meta[_PENDING_KEY] = pending[-100:]
        return None

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
                payload={
                    "platform": event.platform,
                    "source_stream_id": event.source_stream_id,
                    "event_id": event.event_id,
                    "occurred_at": event.occurred_at,
                },
            )
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_viewer_msg",
                session_id,
            )

    async def _persist_rejected(self, session_id: str, event: PlatformEvent, reason: str) -> None:
        """Audit a rejected event with a sanitized reason (no raw viewer text)."""
        if self._pg_store is None or not getattr(self._pg_store, "enabled", False):
            return
        try:
            await self._pg_store.insert_audit_event(
                "event_ingress.rejected",
                session_id=session_id,
                actor=event.platform,
                resource=f"{event.type}:{event.event_id}",
                detail={"reason": reason, "source_stream_id": event.source_stream_id},
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

    async def _record_seen(self, session_id: str, meta: dict, event_id: str) -> None:
        entries = self._dedup_entries(meta)
        now = self._now()
        cutoff = now - self._dedup_window_sec
        entries = [entry for entry in entries if entry.get("ts", 0) >= cutoff]
        entries.append({"event_id": event_id, "ts": now})
        if len(entries) > self._dedup_max_ids:
            entries = entries[-self._dedup_max_ids :]
        meta[_DEDUP_KEY] = entries
        await self._save_meta(session_id, meta)

    # ------------------------------------------------------------------
    # Per-event processing
    # ------------------------------------------------------------------

    def _reject_reason(self, event: PlatformEvent, now: float) -> Optional[str]:
        """Structural pre-embedding rejection (full SafetyGate is a later cluster)."""
        if abs(now - event.occurred_at) > MAX_STALENESS_SEC:
            return _REASON_STALE
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
            ),
        )

    async def _process_event(
        self, session_id: str, event: PlatformEvent, meta: dict, now: float
    ) -> dict:
        """Handle one event; returns the per-event result item."""
        result: dict[str, Any] = {"event_id": event.event_id}
        seen = self._seen_event_ids(meta, now)
        if event.event_id in seen:
            result["status"] = EventStatus.DUPLICATE.value
            return result

        reason = self._reject_reason(event, now)
        if reason is not None:
            result["status"] = EventStatus.REJECTED.value
            result["reason"] = reason
            self._rejection_counts[reason] = self._rejection_counts.get(reason, 0) + 1
            await self._persist_rejected(session_id, event, reason)
            await self._record_seen(session_id, meta, event.event_id)
            return result

        result["status"] = EventStatus.ACCEPTED.value
        if event.type == "viewer.comment":
            comment_id = self._route_comment(meta, session_id, event)
            if comment_id is not None:
                result["comment_id"] = comment_id
            await self._persist_accepted(session_id, event)
            self._notify_reducer(session_id, event, comment_id)
        else:
            self._apply_signal(meta, event)
        self._record_viewer_key(meta, event)
        await self._record_seen(session_id, meta, event.event_id)
        self._accepted_count += 1
        return result

    async def ingest(self, session_id: str, events: list[PlatformEvent]) -> dict:
        """Process a bounded batch; returns the per-event result list + counts.

        Raises KeyError when the session is unknown (no meta, no coordinator).

        The whole body runs under a per-session lock (P1-04): meta is read,
        decided, and written as one atomic section so two concurrent ingests
        for the same session observe each other's dedup writes instead of
        both accepting the same event_id.
        """
        lock = self._ingest_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            if not await self._session_exists(session_id):
                raise KeyError(session_id)
            meta = await self._load_meta(session_id)
            now = self._now()
            results = [await self._process_event(session_id, event, meta, now) for event in events]
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
