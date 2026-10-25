"""Live-follow facts: what the host is doing and what happened to each comment.

The Runtime only OBSERVES here. Nothing in this module chooses a cluster, shapes
spoken content or gates a turn; it turns facts the Director/coordinator already
hold into additive events plus a bounded, recoverable snapshot (contract:
``docs/runbooks/live-follow-events.md``).

Rules that keep it safe on the live path:

* Every public method is synchronous and cheap, and a no-op while
  ``RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED`` is off.
* Events go through a bounded per-session queue drained by a short-lived task, so
  a slow or absent ``/ws/control`` subscriber never delays speech. An overflow
  drops the oldest event and is reported (``dropped_events``) so the API
  reconciles from the snapshot instead of guessing.
* An error in this module is logged and swallowed by ``_safe``; it can never fail
  a turn.
* Events and snapshot carry IDs, codes and counts only: no comment text, viewer
  names, prompts, safety evidence or upstream error text.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from collections import OrderedDict, deque
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, Iterable, Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

ENV_ENABLED = "RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED"
SCHEMA_VERSION = 1
EVENT_PREFIX = "live_follow."

# Normalized, final vocabulary. Anything else is reported as "unknown" and never final.
REASON_CODES = frozenset(
    {
        "topic_recently_answered",
        "unclassified",
        "expired",
        "non_actionable",
        "duplicate_content",
        "safety_filtered",
        "session_closing",
        "session_closed",
        "capacity_exceeded",
        "delivery_failed",
        "turn_cancelled",
        "turn_failed",
    }
)
UNKNOWN_REASON = "unknown"

# Reducer intent -> topic_code shown to the shop owner. An intent outside the table
# (unknown, new, ...) is the generic "product_question": never invent a topic.
TOPIC_CODES = {
    "price": "price",
    "shipping": "shipping",
    "stock": "stock",
    "size_color": "size_color",
    "buy_intent": "order",
    "promotion": "promotion",
    "comparison": "comparison",
    "usage": "usage",
    "complaint": "complaint",
}
GENERIC_TOPIC = "product_question"
NON_ACTIONABLE_INTENTS = frozenset({"spam", "off_topic", "social"})

MAX_MEMBER_IDS = 200  # == ClusterStoreConfig.max_members_per_cluster
CHANGES_PER_EVENT = 50
IDS_PER_EVENT = 200
QUEUE_MAX = 512
COMMENT_HISTORY = 2000
SNAPSHOT_COMMENTS = 200
TURN_HISTORY = 10
MEMBERSHIP_SYNC_MIN_INTERVAL_SEC = 1.0

EmitFn = Callable[[str, dict], Awaitable[None]]


def enabled_from_env() -> bool:
    return os.environ.get(ENV_ENABLED, "").strip().lower() in {"1", "true", "yes", "on"}


def topic_code(intent: Optional[str]) -> str:
    return TOPIC_CODES.get(intent or "", GENERIC_TOPIC)


def normalize_reason(code: Optional[str], final: bool) -> tuple[str, bool]:
    """(reason_code, final): an unrecognised code is "unknown" and never final."""
    if code in REASON_CODES:
        return str(code), final
    return UNKNOWN_REASON, False


def comment_key(provenance: Optional[dict]) -> Optional[str]:
    """Platform identity of a comment: platform/account/external live/message, IDs only."""
    if not provenance:
        return None
    parts = [
        provenance.get(name)
        for name in ("platform", "connected_account_id", "external_session_id", "source_message_id")
    ]
    if not all(parts):
        return None
    return "/".join(quote(str(part), safe="") for part in parts)


def ids_sha256(ids: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for item in sorted(ids):
        digest.update(f"{len(item)}:{item}".encode("utf-8"))
    return digest.hexdigest()


def _rfc3339(epoch: Optional[float] = None) -> str:
    return (
        datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _turn_kind(action: str) -> str:
    if action in ("answer_cluster", "answer_fact"):
        return "qa"
    if action == "autonomous_opening":
        return "opening"
    if action == "close":
        return "closing"
    return "script"


class _NoSession(Exception):
    """The session was never begun or is already retired: nothing to record."""


def _safe(method):
    """Observation must never break the live path."""

    @wraps(method)
    def wrapper(self, *args, **kwargs):
        if not self.enabled:
            return None
        try:
            return method(self, *args, **kwargs)
        except _NoSession:
            return None
        except Exception as exc:  # noqa: BLE001 - never the payload
            logger.warning("live_follow %s failed error=%s", method.__name__, type(exc).__name__)
            return None

    return wrapper


class _Comment:
    __slots__ = (
        "comment_id",
        "event_id",
        "key",
        "state",
        "reason",
        "final",
        "turn_id",
        "cluster_id",
        "seq",
        "ts",
    )

    def __init__(self, comment_id: str, event_id: Optional[str], key: Optional[str]) -> None:
        self.comment_id = comment_id
        self.event_id = event_id
        self.key = key
        self.state = "pending"  # pending | deferred | selected | answered | skipped
        self.reason: Optional[str] = None
        self.final = False
        self.turn_id: Optional[str] = None
        self.cluster_id: Optional[str] = None
        self.seq = 0
        self.ts: Optional[float] = None  # platform time, kept here: the reducer prunes its own

    def view(self) -> dict:
        return {
            "comment_id": self.comment_id,
            "event_id": self.event_id,
            "comment_key": self.key,
            "state": self.state,
            "reason_code": self.reason,
            "final": self.final,
            "turn_id": self.turn_id,
            "cluster_id": self.cluster_id,
            "updated_sequence": self.seq,
        }


class _Session:
    def __init__(self) -> None:
        self.epoch = uuid.uuid4().hex  # a restart changes it, so follow_sequence may restart
        self.identity: dict[str, Optional[str]] = {}
        self.seq = 0
        self.dropped = 0
        self.queue: deque[dict] = deque()
        self.task: Optional[asyncio.Task] = None
        self.send_lock = asyncio.Lock()  # shared across replacements of this session id
        self.comments: "OrderedDict[str, _Comment]" = OrderedDict()  # bounded history
        self.live_comments: dict[str, _Comment] = {}  # pinned by reducer membership
        self.membership: dict[str, str] = {}  # comment_id -> cluster_id (live)
        self.membership_revision = 0
        self.cluster_revision: dict[str, int] = {}
        self.turns: "OrderedDict[str, dict]" = OrderedDict()
        self.position: Optional[dict] = None
        self.media_started: set[str] = set()
        self.held: Optional[str] = None
        self.closing_reason: Optional[str] = None
        self.last_sync = 0.0
        self.tick_deferred: set[str] = set()


class LiveFollowRecorder:
    """Per-process recorder; one `_Session` per runtime session."""

    def __init__(self, emit: Optional[EmitFn] = None, enabled: Optional[bool] = None) -> None:
        self.enabled = enabled_from_env() if enabled is None else bool(enabled)
        self._emit = emit
        self._sessions: dict[str, _Session] = {}
        self._retiring: dict[str, set[_Session]] = {}

    # ------------------------------------------------------------------ plumbing

    def _session(self, session_id: str) -> _Session:
        state = self._sessions.get(session_id)
        if state is None:
            raise _NoSession(session_id)
        return state

    def begin(self, session_id: str) -> None:
        """Start recording for an attached session (a late event of a stopped one is dropped)."""
        if self.enabled and session_id not in self._sessions:
            state = _Session()
            previous = self._retiring.get(session_id)
            if previous:
                state.send_lock = next(iter(previous)).send_lock
            self._sessions[session_id] = state

    def retire(self, session_id: str) -> None:
        """Drop queued facts and cancel the drain, including any in-flight hub send."""
        state = self._sessions.pop(session_id, None)
        if state is None:
            return
        state.queue.clear()
        if state.task is not None and not state.task.done():
            self._retiring.setdefault(session_id, set()).add(state)
            state.task.cancel()

            def finished(task: asyncio.Task) -> None:
                # Retrieve completion without propagating cancellation to another generation.
                if not task.cancelled():
                    task.exception()
                retiring = self._retiring.get(session_id)
                if retiring is not None:
                    retiring.discard(state)
                    if not retiring:
                        self._retiring.pop(session_id, None)

            state.task.add_done_callback(finished)

    def bind(self, session_id: str, identity: Optional[dict]) -> None:
        """Remember tenant/business session/generation for the envelope (IDs only)."""
        if not self.enabled or not identity:
            return
        self.begin(session_id)
        state = self._session(session_id)
        state.identity = {
            "tenant_id": identity.get("tenant_id"),
            "business_session_id": identity.get("business_session_id"),
            "runtime_session_id": identity.get("runtime_session_id") or session_id,
            "generation": identity.get("generation"),
        }

    def _envelope(
        self,
        session_id: str,
        state: _Session,
        kind: str,
        source_event_id: str,
        body: dict,
        occurred_at: Optional[float] = None,
    ) -> dict:
        state.seq += 1
        ident = {"runtime_session_id": session_id, **state.identity}
        return {
            "type": EVENT_PREFIX + kind,
            "schema_version": SCHEMA_VERSION,
            # The epoch prefix keeps ids unique across a restart of the follow stream.
            "source_event_id": f"{state.epoch[:8]}:{source_event_id}",
            "tenant_id": ident.get("tenant_id"),
            "business_session_id": ident.get("business_session_id"),
            "runtime_session_id": ident["runtime_session_id"],
            "generation": ident.get("generation"),
            "stream_epoch": state.epoch,
            "follow_sequence": state.seq,
            "source_occurred_at": _rfc3339(occurred_at),
            "emitted_at": _rfc3339(),
            **body,
        }

    def _publish(
        self,
        session_id: str,
        state: _Session,
        kind: str,
        source_event_id: str,
        body: dict,
        occurred_at: Optional[float] = None,
    ) -> int:
        event = self._envelope(session_id, state, kind, source_event_id, body, occurred_at)
        if len(state.queue) >= QUEUE_MAX:
            state.queue.popleft()
            state.dropped += 1
        state.queue.append(event)
        self._kick(session_id, state)
        return event["follow_sequence"]

    def _kick(self, session_id: str, state: _Session) -> None:
        if self._emit is None or (state.task is not None and not state.task.done()):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): the snapshot still holds the facts
        state.task = loop.create_task(self._drain(session_id, state))

    async def _drain(self, session_id: str, state: _Session) -> None:
        try:
            # A replacement waits for cancellation of the old emit (wait_for also
            # awaits its hub child). Holding this lock until emit unwinds prevents
            # even a slow cancellation handler from delivering OLD after NEW.
            async with state.send_lock:
                while state.queue and self._deliverable(session_id, state):
                    event = state.queue.popleft()
                    await self._emit(session_id, event)
                if not self._deliverable(session_id, state):
                    state.queue.clear()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("live_follow drain failed error=%s", type(exc).__name__)

    def _deliverable(self, session_id: str, state: _Session) -> bool:
        """Generation fence: a replaced or retired session's facts are never sent."""
        return self._sessions.get(session_id) is state

    async def flush(self, session_id: str) -> None:
        state = self._sessions.get(session_id)
        if state is not None and state.task is not None:
            await asyncio.gather(state.task, return_exceptions=True)

    # ------------------------------------------------------------------ comments

    def _comment(self, state: _Session, comment_id: str) -> _Comment:
        comment = state.live_comments.get(comment_id)
        if comment is not None:
            return comment
        comment = state.comments.get(comment_id)
        if comment is None:
            comment = _Comment(comment_id, None, None)
        self._remember(state, comment)
        return comment

    def _remember(self, state: _Session, comment: _Comment) -> None:
        state.comments[comment.comment_id] = comment
        state.comments.move_to_end(comment.comment_id)
        while len(state.comments) > COMMENT_HISTORY:
            evicted, _ = state.comments.popitem(last=False)
            state.tick_deferred.discard(evicted)

    def _known(self, state: _Session, cid: str) -> Optional[_Comment]:
        return state.live_comments.get(cid) or state.comments.get(cid)

    def _set(
        self,
        state: _Session,
        comment: _Comment,
        status: str,
        reason: Optional[str] = None,
        final: bool = False,
        turn_id: Optional[str] = None,
    ) -> None:
        comment.state, comment.reason, comment.final, comment.turn_id = (
            status,
            reason,
            final,
            turn_id,
        )
        comment.seq = state.seq
        if final:
            state.tick_deferred.discard(comment.comment_id)

    @_safe
    def sync_membership(
        self,
        session_id: str,
        membership: dict[str, tuple[str, str]],
        provenance: Callable[[str], Optional[tuple]],
        *,
        now: float,
        horizon_sec: float,
        force: bool = False,
    ) -> None:
        """Diff the store's live comment->cluster map and emit the changes.

        ``membership`` is ``{comment_id: (cluster_id, intent)}``. A comment that left the
        store without an answer gets its final reason here: aged out, or dropped by a bound.
        """
        state = self._session(session_id)
        mono = time.monotonic()
        if not force and mono - state.last_sync < MEMBERSHIP_SYNC_MIN_INTERVAL_SEC:
            return
        state.last_sync = mono
        previous = state.membership
        changes: list[dict] = []
        touched: set[str] = set()
        non_actionable: list[str] = []
        for comment_id, (cluster_id, intent) in membership.items():
            before = previous.get(comment_id)
            if before == cluster_id:
                continue
            comment = self._comment(state, comment_id)
            state.live_comments[comment_id] = comment
            state.comments.pop(comment_id, None)
            record = provenance(comment_id)
            if record is not None:
                comment.event_id = record[0]
                comment.key = comment_key(record[1])
            if comment.ts is None:
                comment.ts = float(record[2]) if record is not None else now
            comment.cluster_id = cluster_id
            touched.add(cluster_id)
            if before is not None:
                touched.add(before)
            changes.append(
                {
                    "comment_id": comment_id,
                    "event_id": comment.event_id,
                    "comment_key": comment.key,
                    "cluster_id": cluster_id,
                    "previous_cluster_id": before,
                    "change": "assigned" if before is None else "reassigned",
                }
            )
            if intent in NON_ACTIONABLE_INTENTS and before is None and not comment.final:
                non_actionable.append(comment_id)
        gone = [cid for cid in previous if cid not in membership]
        expired: list[str] = []
        capacity: list[str] = []
        recently: list[str] = []
        for comment_id in gone:
            comment = self._comment(state, comment_id)
            touched.add(previous[comment_id])
            changes.append(
                {
                    "comment_id": comment_id,
                    "event_id": comment.event_id,
                    "comment_key": comment.key,
                    "cluster_id": None,
                    "previous_cluster_id": previous[comment_id],
                    "change": "removed",
                }
            )
            comment.cluster_id = None
            if comment.final or comment.state == "answered":
                continue
            if comment.state == "selected":
                continue  # its turn is still in flight; the turn outcome settles it
            if comment.reason == "topic_recently_answered":
                recently.append(comment_id)
            elif comment.state == "skipped":
                continue
            else:
                record = provenance(comment_id)
                ts = comment.ts if comment.ts is not None else (record[2] if record else None)
                aged = ts is not None and now - float(ts) >= horizon_sec
                (expired if aged else capacity).append(comment_id)
        state.membership = {cid: cluster for cid, (cluster, _intent) in membership.items()}
        if changes:
            state.membership_revision += 1
            for cluster_id in touched:
                state.cluster_revision[cluster_id] = state.membership_revision
            for index in range(0, len(changes), CHANGES_PER_EVENT):
                chunk = changes[index : index + CHANGES_PER_EVENT]
                for change in chunk:
                    cluster_id = change["cluster_id"] or change["previous_cluster_id"]
                    change["cluster_revision"] = state.cluster_revision.get(cluster_id)
                sequence = self._publish(
                    session_id,
                    state,
                    "comment.cluster_assigned",
                    f"assign:{state.membership_revision}:{index // CHANGES_PER_EVENT}",
                    {"membership_revision": state.membership_revision, "changes": chunk},
                )
                for change in chunk:
                    comment = self._known(state, change["comment_id"])
                    if comment is not None:
                        comment.seq = sequence
        self._skip(session_id, state, non_actionable, "non_actionable", True)
        self._skip(session_id, state, expired, "expired", True)
        self._skip(session_id, state, capacity, "capacity_exceeded", True)
        self._skip(session_id, state, recently, "topic_recently_answered", True)
        # Keep every live member, bounded by the reducer's configured cluster/member
        # limits, independently of the bounded history of members that left.
        for cid in gone:
            comment = state.live_comments.pop(cid, None)
            if comment is not None:
                self._remember(state, comment)
        # Retention: revisions only for clusters that still exist.
        state.cluster_revision = {
            cid: rev
            for cid, rev in state.cluster_revision.items()
            if cid in set(state.membership.values())
        }
        if state.closing_reason is not None:
            self._close_open(session_id, state)  # members that arrive under the fence

    @_safe
    def note_tick_skips(
        self, session_id: str, notes: Iterable[tuple[str, tuple[str, ...]]]
    ) -> None:
        """Cooldown waits seen by one decision tick: a NON-final hint, once per comment.

        size_gate is deliberately not reported: the comment simply keeps waiting.
        """
        state = self._session(session_id)
        waiting = [
            cid
            for reason, ids in notes
            if reason == "topic_cooldown"
            for cid in ids
            if cid not in state.tick_deferred
            and (self._known(state, cid) is None or self._known(state, cid).state == "pending")
        ]
        if waiting:
            state.tick_deferred.update(waiting)
            self._defer(session_id, state, waiting, "topic_recently_answered")

    def _defer(self, session_id: str, state: _Session, ids: list[str], reason: str, turn_id=None):
        fresh = []
        for cid in ids:
            comment = self._comment(state, cid)
            if not comment.final:
                self._set(state, comment, "deferred", reason, False, turn_id)
                fresh.append(cid)
        ids = fresh
        self._chunked(session_id, state, "comment.deferred", ids, reason, False, turn_id)

    def _skip(
        self,
        session_id: str,
        state: _Session,
        ids: list[str],
        reason: str,
        final: bool,
        turn_id: Optional[str] = None,
        event_ids: Iterable[str] = (),
    ) -> None:
        reason, final = normalize_reason(reason, final)
        fresh = []
        for cid in ids:
            comment = self._known(state, cid)
            if comment is None:
                logger.warning("live_follow skip missing comment metadata")
                continue
            if not comment.final:
                self._set(state, comment, "skipped", reason, final, turn_id)
                fresh.append(cid)
        self._chunked(
            session_id, state, "comment.skipped", fresh, reason, final, turn_id, event_ids
        )

    def _chunked(
        self,
        session_id: str,
        state: _Session,
        kind: str,
        ids: list[str],
        reason: str,
        final: bool,
        turn_id: Optional[str],
        event_ids: Iterable[str] = (),
    ) -> None:
        event_ids = list(event_ids)
        if not ids and not event_ids:
            return
        total = max(len(ids), len(event_ids))
        for index in range(0, total, IDS_PER_EVENT):
            part = index // IDS_PER_EVENT
            chunk_ids = ids[index : index + IDS_PER_EVENT]
            chunk_events = event_ids[index : index + IDS_PER_EVENT]
            anchor = (chunk_ids or chunk_events)[0]
            self._publish(
                session_id,
                state,
                kind,
                f"{kind}:{reason}:{turn_id or '-'}:{anchor}:{part}:{state.seq + 1}",
                {
                    "comment_ids": chunk_ids,
                    "event_ids": chunk_events,
                    "reason_code": reason,
                    "final": final,
                    "related_turn_id": turn_id,
                },
            )

    @_safe
    def rejected_before_runtime(self, session_id: str, event_id: str, reason: str) -> None:
        """A comment the Runtime refused at intake (no runtime comment id exists yet)."""
        state = self._session(session_id)
        self._chunked(session_id, state, "comment.skipped", [], reason, True, None, [event_id])

    @_safe
    def close_pending(self, session_id: str, reason: str) -> None:
        """Closing/ended: nothing waiting will be answered. Final for every open comment."""
        state = self._session(session_id)
        state.closing_reason = state.closing_reason or reason
        self._close_open(session_id, state)

    def _close_open(self, session_id: str, state: _Session) -> None:
        waiting = [
            c.comment_id
            for c in (*state.comments.values(), *state.live_comments.values())
            if not c.final and c.state != "answered"
        ]
        self._skip(session_id, state, waiting, state.closing_reason or "session_closed", True)

    # ------------------------------------------------------------------ turns

    @_safe
    def turn_selected(self, session_id: str, decision: Any, ds: Any) -> None:
        state = self._session(session_id)
        if decision.turn_id in state.turns:
            return  # already announced (queue time, then again before speaking): members stay frozen
        kind = _turn_kind(decision.action)
        entry: dict[str, Any] = {
            "turn_id": decision.turn_id,
            "turn_kind": kind,
            "action": decision.action,
            "product_id": decision.product_id,
            "unit_index": decision.unit_index,
            "status": "selected",
            "attempt": 0,
            "outcome_revision": 0,
        }
        if kind == "qa":
            members = list(dict.fromkeys(decision.cluster_member_ids or ()))
            cluster_id = decision.source_cluster_id
            entry.update(
                {
                    "cluster_id": cluster_id,
                    "cluster_revision": state.cluster_revision.get(cluster_id or ""),
                    "membership_revision": state.membership_revision,
                    "topic_code": topic_code(decision.topic),
                    "member_count": len(members),
                    "member_ids": members[:MAX_MEMBER_IDS],
                    "member_ids_truncated": len(members) > MAX_MEMBER_IDS,
                    "member_ids_sha256": ids_sha256(members),
                }
            )
            for cid in members:
                comment = self._comment(state, cid)
                if comment.state != "answered":
                    self._set(state, comment, "selected", None, False, decision.turn_id)
        state.turns[decision.turn_id] = entry
        while len(state.turns) > TURN_HISTORY * 4:
            state.turns.popitem(last=False)
        body = {
            k: v for k, v in entry.items() if k not in ("status", "attempt", "outcome_revision")
        }
        self._publish(
            session_id, state, "cluster.turn_selected", f"selected:{decision.turn_id}", body
        )
        if state.position is None or state.position["phase"] in ("idle", "preparing"):
            self._position(session_id, state, ds, "preparing", decision)

    def _terminal(
        self,
        session_id: str,
        state: _Session,
        decision: Any,
        status: str,
        reason: str,
        occurred_at: Optional[float] = None,
        **extra: Any,
    ) -> Optional[dict]:
        turn = state.turns.get(decision.turn_id)
        if turn is None or turn["status"] in ("completed", "cancelled", "failed"):
            return None  # never announced, or the first terminal outcome already stands
        turn["status"] = status
        turn["outcome_revision"] += 1
        turn["attempt"] = int(getattr(decision, "attempt", 0) or 0)
        turn.update(extra)
        body = {
            "turn_id": decision.turn_id,
            "turn_kind": turn["turn_kind"],
            "attempt": turn["attempt"],
            "outcome_revision": turn["outcome_revision"],
            "reason_code": reason,
            "product_id": decision.product_id,
        }
        if turn["turn_kind"] == "qa":
            body.update(
                {
                    "cluster_id": turn.get("cluster_id"),
                    "member_count": turn["member_count"],
                    "member_ids_sha256": turn["member_ids_sha256"],
                    "topic_code": turn["topic_code"],
                    "coverage_scope": "cluster",
                }
            )
        body.update(extra)
        self._publish(
            session_id,
            state,
            f"turn.{status}",
            f"turn:{decision.turn_id}:{status}:{turn['outcome_revision']}",
            body,
            occurred_at,
        )
        return turn

    @_safe
    def turn_completed(
        self, session_id: str, decision: Any, ds: Any, *, response_kind: str, completed_at: float
    ) -> None:
        state = self._session(session_id)
        turn = self._terminal(
            session_id,
            state,
            decision,
            "completed",
            "completed",
            occurred_at=completed_at,
            completed_at=_rfc3339(completed_at),
            response_kind=response_kind,
        )
        if turn is not None and turn["turn_kind"] == "qa":
            # Exactly the members frozen at selection: later arrivals never inherit this.
            for cid in turn["member_ids"]:
                self._set(
                    state, self._comment(state, cid), "answered", None, True, decision.turn_id
                )
        self._position(session_id, state, ds, "preparing", None)

    @_safe
    def turn_ended(
        self, session_id: str, decision: Any, ds: Any, *, status: str, reason: str
    ) -> None:
        """Cancelled or failed. Members wait again unless they already left the store."""
        state = self._session(session_id)
        turn = self._terminal(session_id, state, decision, status, reason)
        if turn is None:
            return
        if turn["turn_kind"] == "qa":
            code = "turn_cancelled" if status == "cancelled" else "turn_failed"
            if reason == "hard_expired":
                code = "expired"
            members = turn["member_ids"]
            gone = [cid for cid in members if cid not in state.membership]
            if reason == "hard_expired":
                self._skip(session_id, state, members, "expired", True, decision.turn_id)
            else:
                self._skip(session_id, state, gone, code, True, decision.turn_id)
                self._defer(
                    session_id,
                    state,
                    [c for c in members if c in state.membership],
                    code,
                    decision.turn_id,
                )
        if state.position is not None and state.position.get("turn_id") == decision.turn_id:
            self._position(session_id, state, ds, "preparing", None)

    # ------------------------------------------------------------------ position

    @_safe
    def speak_started(self, session_id: str, decision: Any, ds: Any) -> None:
        state = self._session(session_id)
        phase = "answering_comment" if _turn_kind(decision.action) == "qa" else "speaking"
        self._position(session_id, state, ds, phase, decision)

    @_safe
    def media_started(self, session_id: str, decision: Any, ds: Any) -> None:
        state = self._session(session_id)
        if decision.turn_id in state.media_started:
            return
        state.media_started.add(decision.turn_id)
        if len(state.media_started) > 64:
            state.media_started = {decision.turn_id}
        if state.position is not None and state.position.get("turn_id") == decision.turn_id:
            self._position(session_id, state, ds, state.position["phase"], decision)

    @_safe
    def hold(self, session_id: str, ds: Any, reason: Optional[str]) -> None:
        state = self._session(session_id)
        if state.held == reason:
            return
        state.held = reason
        if reason == "held":
            self._position(session_id, state, ds, "holding", None)
        elif reason is None:
            self._position(session_id, state, ds, "preparing", None)

    def _script_cursor(self, ds: Any) -> Optional[dict]:
        director = getattr(ds, "director", None)
        if director is None:
            return None
        current = director.state.current_product()
        if current is None:
            return None
        units = tuple(current.units or ())
        return {
            "product_id": current.product_id,
            "next_unit_index": current.next_unit if units else None,
            "unit_count": len(units) or None,
            "completed_units": current.next_unit if units else None,
        }

    def _segment(self, ds: Any, product_id: Optional[str]) -> Optional[dict]:
        envelope = getattr(ds, "approved_envelope", None)
        if envelope is None:
            return None
        product = next(
            (p for p in getattr(envelope, "products", ()) if p.product_id == product_id), None
        )
        return {
            "script_set_id": getattr(envelope, "script_set_id", None),
            "envelope_hash": getattr(envelope, "fingerprint", None),
            "approved_version_id": getattr(product, "approved_version_id", None),
        }

    def _position(
        self, session_id: str, state: _Session, ds: Any, phase: str, decision: Any
    ) -> None:
        cursor = self._script_cursor(ds)
        if state.held == "held":
            phase = "holding"
        elif phase == "preparing" and decision is None and cursor is not None:
            remaining = cursor["unit_count"] is not None and (
                cursor["next_unit_index"] < cursor["unit_count"]
            )
            phase = "preparing" if remaining or cursor["unit_count"] is None else "idle"
        position: dict[str, Any] = {
            "phase": phase,
            "turn_id": None,
            "turn_kind": None,
            "action": None,
            "product_id": cursor["product_id"] if cursor else None,
            "unit_index": None,
            "unit_count": cursor["unit_count"] if cursor else None,
            "unit_text_sha256": None,
            "part_id": None,
            "segment": self._segment(ds, cursor["product_id"] if cursor else None),
            "script_cursor": cursor,
            "topic_code": None,
            "cluster_id": None,
            "member_count": None,
            "media_started": False,
            "hold_reason": state.held,
        }
        if decision is not None:
            turn = state.turns.get(decision.turn_id, {})
            position.update(
                {
                    "turn_id": decision.turn_id,
                    "turn_kind": _turn_kind(decision.action),
                    "action": decision.action,
                    "product_id": decision.product_id or position["product_id"],
                    "unit_index": decision.unit_index,
                    "topic_code": turn.get("topic_code"),
                    "cluster_id": turn.get("cluster_id"),
                    "member_count": turn.get("member_count"),
                    "media_started": decision.turn_id in state.media_started,
                }
            )
            if decision.unit_index is not None and cursor is not None:
                position["segment"] = self._segment(ds, decision.product_id)
                product = next(
                    (p for p in ds.director.state.products if p.product_id == decision.product_id),
                    None,
                )
                units = tuple(getattr(product, "units", ()) or ())
                if units and decision.unit_index < len(units):
                    position["unit_count"] = len(units)
                    position["unit_text_sha256"] = hashlib.sha256(
                        units[decision.unit_index].encode("utf-8")
                    ).hexdigest()
        stable = {k: v for k, v in position.items()}
        if state.position == stable:
            return
        state.position = stable
        self._publish(
            session_id,
            state,
            "position.updated",
            f"position:{state.seq + 1}",
            {"position": stable},
        )

    # ------------------------------------------------------------------ recovery

    def snapshot(self, session_id: str, ds: Any = None) -> dict:
        """Bounded current state for the API to reconcile (watermark = follow_sequence)."""
        if not self.enabled:
            return {"schema_version": SCHEMA_VERSION, "enabled": False}
        state = self._session(session_id)
        comments = sorted(
            (*state.comments.values(), *state.live_comments.values()), key=lambda c: c.seq
        )[-SNAPSHOT_COMMENTS:]
        turns = list(state.turns.values())[-TURN_HISTORY:]
        position = state.position
        if position is None and ds is not None:
            position = {"phase": "preparing", "script_cursor": self._script_cursor(ds)}
        ident = {"runtime_session_id": session_id, **state.identity}
        return {
            "schema_version": SCHEMA_VERSION,
            "enabled": True,
            "tenant_id": ident.get("tenant_id"),
            "business_session_id": ident.get("business_session_id"),
            "runtime_session_id": ident["runtime_session_id"],
            "generation": ident.get("generation"),
            "stream_epoch": state.epoch,
            "watermark": state.seq,
            "dropped_events": state.dropped,
            "membership_revision": state.membership_revision,
            "observed_at": _rfc3339(),
            "position": position,
            "comments": [c.view() for c in comments],
            "turns": [dict(t) for t in turns],
            "limits": {
                "comments": SNAPSHOT_COMMENTS,
                "turns": TURN_HISTORY,
                "member_ids_per_turn": MAX_MEMBER_IDS,
            },
        }
