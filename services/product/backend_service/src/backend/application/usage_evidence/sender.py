"""HMAC signer, background sender and staged-row sweeper (C-USAGE-EVIDENCE-001).

The sender is a background task: nothing here runs on the speech/decision path.

Assumption (shared with the request path): EVERY writer of the session meta holds the
per-session lock. The commit proof (``usage_evidence_commits``), the deferred facts
(``usage_evidence_unstaged``) and the execution state live in that one meta, so an unlocked
read-modify-write elsewhere could rewind them. Pre-existing unlocked writers in
``api/v1/sessions.py`` (attach/binding paths) are outside this slice and are a documented risk.
It matches the existing API receiver exactly: lowercase hex
``HMAC-SHA256(secret, X-AI-Timestamp + "." + raw body)``, unix-second timestamp,
``X-AI-Event-Id`` equal to the body ``event_id``, and the stored bytes resent
verbatim on every attempt (only the timestamp and signature change).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import random
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from backend.application.db.session_store import delete_owned
from backend.application.execution_contract import ExecutionIdentity, ExecutionState
from backend.application.terminal_outcomes import backoff_seconds

from . import envelope
from .outbox import StageBlocked, UsageOutbox  # noqa: F401
from .settings import CogsBuffer, UsageEvidenceSettings

logger = logging.getLogger(__name__)

# Session-meta key: {"version": V, "tokens": {stage_token: commit_version}}, written by the
# SAME atomic save as the state. Tokens leave it only once their row is no longer staged.
COMMITS_KEY = "usage_evidence_commits"
MAX_PENDING_TOKENS = 512
# Facts of a safety command (Emergency End) that could not be staged because the outbox was
# unavailable. Written in the SAME atomic save as the state; staged later by the sweeper.
UNSTAGED_KEY = "usage_evidence_unstaged"
MAX_UNSTAGED = 64
# Set by Stop when rows stay unresolved: the meta (the only commit proof) is kept and the
# sweeper deletes it once the session has no unresolved row left.
CLEANUP_KEY = "usage_evidence_cleanup"
TRANSIENT_PARK_CAP = 300.0  # seconds; proof_unavailable keeps the 1 h cap
# A session meta that holds unresolved evidence (deferred facts, cleanup marker, commit proof of
# a staged row) is the only copy of it: it outlives the 24 h default and is refreshed on every
# sweep/retry; an ERROR is logged when it is within EXPIRY_WARN seconds of expiring anyway.
EVIDENCE_TTL = 7 * 24 * 3600
EXPIRY_WARN = 6 * 3600
# A deferred fact that STILL cannot be staged for a PERMANENT reason (an unresolved attempt owns
# its row, or the entry is malformed) after this long is given up (ERROR, counter) so it cannot
# block later facts forever. An outage (connection errors) never counts: those wait for recovery.
GIVE_UP_AFTER = 24 * 3600


def holds_evidence(meta: dict[str, Any] | None) -> bool:
    """True when this session meta is the only copy of unresolved usage evidence.

    Decided from the meta alone (no database read): deferred facts, the stop-time cleanup
    marker, 018's unstaged terminal fact, or a commit proof that still lists attempts.
    """
    from backend.application.budget_lease import UNSTAGED_KEY as LEASE_UNSTAGED_KEY

    meta = meta or {}
    return bool(
        meta.get(UNSTAGED_KEY)
        or meta.get(CLEANUP_KEY)
        or meta.get(LEASE_UNSTAGED_KEY)
        or (meta.get(COMMITS_KEY) or {}).get("tokens")
    )


_IDENTITY_KEYS = ("tenant_id", "business_session_id", "runtime_session_id", "generation")
_SWEEP_LIMIT = 100
_SWEEP_BATCHES = 10


@asynccontextmanager
async def _no_lock(_session_id: str) -> AsyncIterator[None]:
    yield


PostFn = Callable[[str, bytes, Mapping[str, str]], Awaitable[int]]


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def signed_headers(secret: str, event_id: str, body: bytes, now: float) -> dict[str, str]:
    timestamp = str(int(now))
    return {
        "Content-Type": "application/json",
        "X-AI-Event-Id": event_id,
        "X-AI-Timestamp": timestamp,
        "X-AI-Signature": sign(secret, timestamp, body),
    }


class HttpxPoster:
    """One reused client; closed with the sender. Redirects are never followed."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._client: Any = None

    async def __call__(self, url: str, body: bytes, headers: Mapping[str, str]) -> int:
        if self._client is None:
            import httpx  # already a runtime dependency

            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=False)
        response = await self._client.post(url, content=body, headers=dict(headers))
        return response.status_code

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


class UsageSender:
    def __init__(
        self,
        outbox: UsageOutbox,
        settings: UsageEvidenceSettings,
        *,
        session_store: Any = None,
        session_lock: Callable[[str], Any] | None = None,
        cogs: CogsBuffer | None = None,
        post: PostFn | None = None,
        unstaged_sessions: set[str] | None = None,
        clock: Callable[[], float] = time.time,
        rng: Callable[[], float] = random.random,
        batch: int = 10,
    ) -> None:
        self._outbox = outbox
        self._settings = settings
        self._store = session_store
        self._unstaged: set[str] = unstaged_sessions if unstaged_sessions is not None else set()
        self._discovered = False
        self.unstageable_facts = 0  # deferred facts given up (health / operator)
        # Must be the SAME lock the request path holds from staging through the save.
        self._session_lock = session_lock or _no_lock
        self._cogs = cogs
        self._poster = HttpxPoster(settings.http_timeout) if post is None else None
        self._post: PostFn = post or self._poster  # type: ignore[assignment]
        self._clock = clock
        self._rng = rng
        self._batch = batch
        self._last_sweep = 0.0
        self.permanent_failures = 0

    # -- one delivery pass -------------------------------------------------

    async def deliver_due(self) -> int:
        rows = await self._outbox.claim(self._batch, self._settings.lease_seconds)
        for row in rows:
            await self._deliver(row)
        return len(rows)

    async def _deliver(self, row: Mapping[str, Any]) -> None:
        eid, token, attempts = row["event_id"], row["lease_token"], int(row["attempts"])
        body = bytes(row["body"])
        headers = signed_headers(self._settings.secret, eid, body, self._clock())

        async def finish(status: str, *, http: int | None, error: str | None, retry: float = 0.0):
            ok = await self._outbox.finish(
                eid, token, status, http_status=http, error=error, retry_in=retry
            )
            if not ok:
                logger.warning("usage evidence lease lost id=%s; outcome discarded", eid)

        def retry_delay() -> float:
            return backoff_seconds(
                attempts,
                base=self._settings.backoff_base,
                cap=self._settings.backoff_cap,
                rng=self._rng,
            )

        try:
            status = await self._post(self._settings.url, body, headers)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # timeout, refused, DNS: never dropped
            await finish(
                "ready", http=None, error=f"transport_{type(exc).__name__}", retry=retry_delay()
            )
            return
        if status == 202:  # accepted, or duplicate / already_processing: durably claimed
            await finish("delivered", http=status, error=None)
        elif status == 409:  # same event_id, different bytes: a producer bug; never resend
            self.permanent_failures += 1
            logger.error("usage evidence CONFLICT id=%s; marked permanent, not resent", eid)
            await finish("conflict", http=status, error="http_409")
        elif status in (400, 413):
            self.permanent_failures += 1
            logger.error("usage evidence rejected id=%s status=%s; not retried", eid, status)
            await finish("rejected", http=status, error=f"http_{status}")
        else:  # 401 (clock/secret), 503, 5xx, anything unexpected: retry, re-signed
            if status == 401:
                logger.error("usage evidence 401 id=%s: check clock skew or secret rotation", eid)
            await finish("ready", http=status, error=f"http_{status}", retry=retry_delay())

    # -- staged-row sweeper (Redis meta <-> Postgres commit gap) -------------

    async def sweep(self) -> int:
        """Resolve staged rows older than the configured age, with proof of the attempt.

        Per row, under the SAME per-session lock the request path holds from staging
        through the save: re-read the row (token + status compare-and-set), then the session.
        Token in the session proof -> the exact stored payload committed: ``mark_ready``
        (numbered in commit order). Token absent from a readable session of the same
        identity -> that attempt provably never committed (the lock excludes any save in
        flight): discard. ANYTHING ELSE (session missing, unreadable, other generation) is
        ``proof_unavailable``: the row is parked with backoff and an ERROR log, exposed in
        health, and NEVER discarded or dropped on a timer. A busy lock defers the row with
        a jittered exponential backoff so it cannot starve others; each sweep has a
        wall-clock budget. Finally finished rows past retention are deleted.
        """
        if self._store is None:
            return 0
        await self.refresh_held()  # FIRST, with no database call: nothing below can skip it
        await self.drain_unstaged()
        resolved = 0
        deadline = time.monotonic() + self._settings.sweep_budget
        for _ in range(_SWEEP_BATCHES):
            rows = await self._outbox.stale_staged(self._settings.sweep_age, _SWEEP_LIMIT)
            if not rows:
                break
            resolved += await self._resolve(rows, deadline)
            if len(rows) < _SWEEP_LIMIT or time.monotonic() >= deadline:
                break
        try:
            await self._outbox.purge(self._settings.retention_days, self._settings.retention_batch)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("usage evidence retention failed error_type=%s", type(exc).__name__)
        return resolved

    async def refresh_held(self) -> int:
        """Refresh the TTL of every session meta that holds unresolved evidence.

        Decided from the metas alone, so it works through a FULL database outage. A session
        that cannot be refreshed now (lock busy, store hiccup) is retried next sweep.
        ponytail: scans all live sessions each sweep; a tracked-set index if that gets large.
        """
        lister = getattr(self._store, "list_session_ids", None)
        ids = set(self._unstaged)
        try:
            ids.update(await lister() if lister else ())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("usage ttl refresh listing failed error_type=%s", type(exc).__name__)
        done = 0
        for sid in ids:
            try:
                async with self._session_lock(sid) as fence:
                    meta = await self._store.get(sid)
                    if (meta or {}).get(UNSTAGED_KEY) or (meta or {}).get(CLEANUP_KEY):
                        # Another replica may crash after saving a fact but before track().
                        # Discover its durable work on every scan, not only at startup.
                        self.track(sid)
                    if holds_evidence(meta):
                        await self._keep_alive(sid, meta, fence)
                        done += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("usage ttl refresh deferred error_type=%s", type(exc).__name__)
        return done

    def track(self, session_id: str) -> None:
        """Session meta kept for deferred work (unstaged facts / cleanup marker): sweep it."""
        self._unstaged.add(session_id)

    async def resolve_session(self, runtime_session_id: str, fence: Any = None) -> int:
        """Resolve every unresolved row of one session. The CALLER holds the session lock.

        Used by stop/cleanup BEFORE the session meta (the only copy of the commit proof) is
        deleted. Returns how many rows/facts REMAIN unresolved (0 = safe to delete the meta);
        raises on an infrastructure error, which the caller must treat as "unresolved".
        """
        await self._drain_one(runtime_session_id, fence)
        rows = await self._outbox.staged_for_session(runtime_session_id)
        remaining = 0
        for row in sorted(rows, key=lambda r: int(r["staged_version"])):
            outcome = await self._resolve_one(row, fence)
            if outcome not in ("ready", "discarded"):
                remaining += 1
                logger.error(
                    "usage evidence rows left unresolved at session cleanup session=%s id=%s "
                    "outcome=%s",
                    runtime_session_id,
                    row["event_id"],
                    outcome,
                )
        meta = await self._store.get(runtime_session_id)
        remaining += len((meta or {}).get(UNSTAGED_KEY) or [])
        return remaining

    async def drain_unstaged(self) -> int:
        """Stage facts deferred while the outbox was down (Emergency End must never block)."""
        if self._store is None:
            return 0
        if not self._discovered:
            self._discovered = True
            lister = getattr(self._store, "list_session_ids", None)
            try:
                for sid in await lister() if lister else ():
                    meta = await self._store.get(sid)
                    if (meta or {}).get(UNSTAGED_KEY) or (meta or {}).get(CLEANUP_KEY):
                        self._unstaged.add(sid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._discovered = False  # try again next sweep
                logger.warning("usage unstaged discovery failed error_type=%s", type(exc).__name__)
        done = 0
        for sid in list(self._unstaged):
            try:
                done += await self.drain_session(sid)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("usage unstaged drain deferred error_type=%s", type(exc).__name__)
        return done

    async def drain_session(self, sid: str) -> int:
        """Drain one session's deferred facts under its lock (sweeper, and a bounded attempt
        right after a safety command). Keeps the meta alive while anything is unresolved."""
        async with self._session_lock(sid) as fence:
            held = await self._store.get(sid)
            if holds_evidence(held):  # FIRST: before any database call (a lost fence aborts)
                await self._keep_alive(sid, held, fence)
            done = await self._drain_one(sid, fence)
            await self._cleanup_if_done(sid, await self._store.get(sid), fence)  # Stop kept it
            meta = await self._store.get(sid)
            if meta is None or not (meta.get(UNSTAGED_KEY) or meta.get(CLEANUP_KEY)):
                self._unstaged.discard(sid)
            return done

    async def _keep_alive(self, sid: str, meta: dict[str, Any], fence: Any) -> None:
        """Refresh the TTL of a meta that holds unresolved evidence (caller holds the lock)."""
        ttl_left = getattr(self._store, "ttl_remaining", None)
        if ttl_left is not None:
            try:
                left = await ttl_left(sid)
            except asyncio.CancelledError:
                raise
            except Exception:
                left = None
            if left is not None and 0 <= left < EXPIRY_WARN:
                logger.error(
                    "session with UNRESOLVED usage evidence is %.0f s from expiry session=%s",
                    left,
                    sid,
                )
        await self._write(sid, meta, fence)

    async def _write(self, sid: str, meta: dict[str, Any], fence: Any) -> None:
        if fence is not None and hasattr(self._store, "commit_if_owner"):
            if not await self._store.commit_if_owner(fence, meta, ttl_seconds=EVIDENCE_TTL):
                raise RuntimeError("session lock lost while writing usage evidence meta")
        else:
            await self._store.set(sid, meta, ttl_seconds=EVIDENCE_TTL)

    async def _drain_one(self, sid: str, fence: Any) -> int:
        """Stage one session's deferred facts. Runs under the session lock.

        Each fact is staged (idempotent by semantic key), its token goes into the commit proof
        and the entry leaves the meta in ONE save, then the row is flipped ready. A fact whose
        row is already final just leaves the meta; a blocked one stays for the next pass.
        """
        meta = await self._store.get(sid)
        entries = list((meta or {}).get(UNSTAGED_KEY) or [])
        if not entries:
            return 0
        raw = meta.get(COMMITS_KEY) or {}
        version = int(raw.get("version", 0))
        tokens = dict(raw.get("tokens") or {})
        remaining: list[dict[str, Any]] = []
        flips: list[tuple[str, str]] = []
        for entry in entries:
            try:
                identity = ExecutionIdentity(**entry["identity"])
                staged = await self._outbox.stage(
                    identity,
                    [envelope.draft_from_dict(entry["draft"])],
                    version=version + 1,
                    committed_tokens=set(tokens),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # StageBlocked, database down: keep it for the next pass
                logger.warning("usage unstaged fact kept error_type=%s", type(exc).__name__)
                if self._give_up(entry, exc):
                    continue  # permanently unstageable for hours: later facts go through
                # Order: a later fact never overtakes an earlier one that could not be staged.
                remaining.extend(entries[entries.index(entry) :])
                break
            for st in staged:
                if st.token:
                    version += 1
                    tokens[st.token] = st.version
                    flips.append((st.event_id, st.token))
        if len(remaining) == len(entries):
            return 0
        meta[COMMITS_KEY] = {"version": version, "tokens": tokens}
        if remaining:
            meta[UNSTAGED_KEY] = remaining
        else:
            meta.pop(UNSTAGED_KEY, None)
        await self._write(sid, meta, fence)
        try:
            await self._outbox.mark_ready(flips)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # the token is in the proof: the sweeper converges
            logger.warning("usage unstaged ready flip deferred error_type=%s", type(exc).__name__)
        return len(entries) - len(remaining)

    def _give_up(self, entry: dict[str, Any], exc: Exception) -> bool:
        """True when a deferred fact failed for a PERMANENT reason for more than GIVE_UP_AFTER."""
        permanent = isinstance(
            exc, (StageBlocked, envelope.InvalidIdentity, ValueError, KeyError, TypeError)
        )
        now = self._clock()
        first = entry.setdefault("deferred_at", now)  # stamped on first sight if absent
        if not permanent or now - float(first) < GIVE_UP_AFTER:
            return False
        self.unstageable_facts += 1
        logger.error(
            "usage evidence fact UNSTAGEABLE for %.0f h, given up so later facts can proceed "
            "kind=%s error_type=%s",
            (now - float(first)) / 3600,
            (entry.get("draft") or {}).get("kind"),
            type(exc).__name__,
        )
        return True

    async def _cleanup_if_done(
        self, sid: str, meta: dict[str, Any] | None, fence: Any = None
    ) -> None:
        """Stop kept the meta because rows were unresolved: delete it once none are left."""
        if not (meta or {}).get(CLEANUP_KEY):
            return
        if await self._outbox.staged_for_session(sid) or (meta or {}).get(UNSTAGED_KEY):
            return
        from backend.application.budget_lease import UNSTAGED_KEY as LEASE_UNSTAGED_KEY

        if (meta or {}).get(LEASE_UNSTAGED_KEY):  # 018's own terminal fact still owes staging
            return
        await delete_owned(self._store, sid, fence)  # never delete under a lost lease
        self._unstaged.discard(sid)

    async def _resolve(self, rows: list[dict[str, Any]], deadline: float) -> int:
        # Commit order within an identity: lower staged_version first.
        rows = sorted(
            rows, key=lambda r: (tuple(r[k] for k in _IDENTITY_KEYS), int(r["staged_version"]))
        )
        done = 0
        parked: dict[str, list[str]] = {}
        busy: list[str] = []
        for row in rows:
            if time.monotonic() >= deadline:
                break  # the remaining rows stay due and are picked up by the next sweep
            try:
                async with self._session_lock(row["runtime_session_id"]) as fence:
                    outcome = await self._resolve_one(row, fence)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # lock busy/timeout, store or DB hiccup
                logger.warning("usage sweep deferred error_type=%s", type(exc).__name__)
                busy.append(row["event_id"])
                continue
            if outcome in ("ready", "discarded"):
                done += 1
            elif outcome is not None:
                parked.setdefault(outcome, []).append(row["event_id"])
        if busy:
            await self._outbox.defer(
                busy, base=self._settings.busy_backoff_base, cap=self._settings.busy_backoff_cap
            )
        for reason, ids in parked.items():
            cap = 3600.0 if reason.startswith("proof_unavailable") else TRANSIENT_PARK_CAP
            await self._outbox.park(ids, reason=reason, retry_in=self._settings.sweep_age, cap=cap)
        return done

    async def _resolve_one(self, row: dict[str, Any], fence: Any = None) -> str | None:
        """Decide one row. Runs under the session lock. Returns an outcome or a park reason."""
        eid, token = row["event_id"], row["stage_token"]
        reason = None
        meta = None
        try:
            meta = await self._store.get(row["runtime_session_id"])
            raw = (meta or {}).get("execution_contract")
            state = ExecutionState.model_validate(raw) if raw else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("usage sweep unreadable error_type=%s", type(exc).__name__)
            return "session_unreadable"  # transient: retried with the short backoff cap
        if meta is not None:
            # An unresolved row: its commit proof must not expire. FIRST, before any database
            # call. A failed or refused fenced write means the lock is lost: ABORT (the caller
            # defers the row); nothing below may act on this possibly stale snapshot.
            await self._keep_alive(row["runtime_session_id"], meta, fence)
        fresh = await self._outbox.get_staged(eid)
        if fresh is None or fresh["status"] != "staged" or fresh["stage_token"] != token:
            return None  # resolved or replaced meanwhile: that attempt owns its own fate
        if state is None and reason is None:
            reason = "session_missing"
        elif state is not None and (
            envelope.identity_of(state).model_dump() != {k: row[k] for k in _IDENTITY_KEYS}
        ):
            reason = "generation_changed"
        if reason is not None:
            # Unknown commit status: never discard, never drop on a timer.
            logger.error(
                "usage evidence proof_unavailable id=%s reason=%s: row kept staged for an "
                "operator (it may be a committed fact)",
                eid,
                reason,
            )
            return "proof_unavailable:" + reason
        tokens = ((meta or {}).get(COMMITS_KEY) or {}).get("tokens") or {}
        if token in tokens:
            flipped = await self._outbox.mark_ready([(eid, token)])
            # Committed, but numbering waits (commit order) for an earlier unresolved row.
            if flipped:
                await self._cleanup_if_done(row["runtime_session_id"], meta, fence)
            return "ready" if flipped else "waiting_for_earlier_row"
        # "Never committed" is decided from this snapshot: prove we still own the lock first.
        await self._keep_alive(row["runtime_session_id"], meta, fence)
        await self._outbox.release([(eid, token)], delete=False)
        logger.warning("usage evidence discarded a never-committed attempt id=%s", eid)
        await self._cleanup_if_done(row["runtime_session_id"], meta, fence)
        return "discarded"

    async def drain_cogs(self) -> int:
        if self._cogs is None:
            return 0
        stored = 0
        for sample in self._cogs.drain(self._batch):
            try:
                eid, body = envelope.build_cogs_body(
                    sample.identity,
                    sample_id=sample.sample_id,
                    occurred_at=sample.occurred_at,
                    model_id=sample.model_id,
                    input_tokens=sample.input_tokens,
                    output_tokens=sample.output_tokens,
                    producer_id=self._settings.producer_id,
                )
                await self._outbox.enqueue_cogs(
                    sample.identity,
                    event_id=eid,
                    sample_id=sample.sample_id,
                    occurred_at=sample.occurred_at,
                    body=body,
                )
                stored += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # COGS is internal and may be lost; never retried here
                self._cogs.lost += 1
                logger.warning("usage cogs sample lost error_type=%s", type(exc).__name__)
        return stored

    async def run_loop(self) -> None:
        while True:
            try:
                if self._clock() - self._last_sweep >= self._settings.sweep_interval:
                    self._last_sweep = self._clock()
                    await self.sweep()
                await self.drain_cogs()
                processed = await self.deliver_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                processed = 0
                logger.warning("usage evidence pass failed error_type=%s", type(exc).__name__)
            if processed == 0:
                await asyncio.sleep(self._settings.poll_seconds)

    async def flush(self, timeout: float) -> None:
        """Bounded final delivery pass at shutdown. Unsent rows stay durable."""

        async def passes() -> None:
            await self.drain_cogs()
            while await self.deliver_due():
                pass

        try:
            await asyncio.wait_for(passes(), timeout=timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # timeout included
            logger.warning(
                "usage evidence final flush incomplete error_type=%s", type(exc).__name__
            )

    async def close(self) -> None:
        if self._poster is not None:
            await self._poster.close()
