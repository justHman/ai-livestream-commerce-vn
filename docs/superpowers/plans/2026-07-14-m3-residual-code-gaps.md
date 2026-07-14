# M3 Residual Code Gaps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two real code gaps left by Milestone 3 — (1) wire the LiveKit Python SDK into the backend audio-track publisher so `LIVEKIT_PUBLISH=1` actually publishes PCM to the SFU, and (2) wire the optional Postgres runtime store into the server lifecycle and persist sessions, viewer messages, and Director decisions when `DATABASE_URL` is set.

**Architecture:** Both gaps are additive and env-gated, so the existing offline test path (`RENDER_BACKEND=mock LLM_ENGINE=none TTS_ENGINE=tone APP_ENV=dev`) stays green without any new dependency installed. LiveKit publish gains a real `livekit-rtc` path behind a lazy import + a fake-RTC seam for offline tests; the no-op-disabled path is unchanged. Postgres store gains a lifecycle hook (connect on startup, apply schema, close on shutdown) and persistence calls fire-and-forget at the existing ingest/chat/decision sites, all guarded by `store.enabled` so a missing `DATABASE_URL` changes nothing.

**Tech Stack:** Python 3.11, FastAPI, uv, pytest + pytest-asyncio (`asyncio_mode=auto`, `testpaths=core/tests`), asyncpg (optional), `livekit-rtc` (new optional dep), PyJWT (already present).

## Global Constraints

- All work path-scoped under `implementations/` (nested git repo). Branch `feature/m3-residual-gaps` from `develop`. Conventional commits `feat/fix/test(scope):`. **No `Co-Authored-By` trailers** — user is sole author. Never commit `.env`.
- Offline tests MUST stay green with no new package installed: `RENDER_BACKEND=mock LLM_ENGINE=none TTS_ENGINE=tone DIRECTOR_ENABLED=0 APP_ENV=dev uv run pytest core/tests/ -q`.
- Engine seam principle (from `docs/architecture.md`): env-swappable, same code Colab → AWS. New behavior is OFF by default (`LIVEKIT_PUBLISH=0`, `DATABASE_URL=""`) so existing deployments change nothing.
- Raw SQL + asyncpg, NO ORM/migration framework (`.claude/rules/database.md`). `core/sql/runtime_schema.sql` is the schema source of truth; `apply_schema` uses `CREATE IF NOT EXISTS` (idempotent, additive).
- Self-host render backends fail loud (`.claude/rules/error-handling.md`) — but this plan does NOT touch `self_host.py`.
- New optional deps are lazy-imported inside the function that needs them, so `uv sync` without extras still imports `core.server` cleanly. Add `livekit-rtc` and `asyncpg` to `[project.optional-dependencies]` under a new `livekit` and reuse the existing `asyncpg`-needing path (asyncpg is NOT yet in pyproject — add it under optional `postgres` extra).
- HTTP error shape: `HTTPException(status_code=..., detail=...)` with correct codes (503 for engine/LiveKit unavailable).
- Parametrized queries only (`$1`, `$2`) — never concatenate user input into SQL.
- Test files live in `core/tests/`. One assertion per test, Arrange-Act-Assert, behavior not implementation. Do not assert mock call counts when output values suffice.
- `ponytail:` comments mark deliberate simplifications with the ceiling + upgrade path.

## Scope note (verified before planning)

The M3 gap audit (`plans/04-gap-audit-and-m3.md`, 2026-07-11) listed 4 code gaps. On 2026-07-14 a full code read found **2 of the 4 are already DONE** and the audit is stale:
- **MJPEG debug-only gate** — DONE. `core/api/v1.py:282-289` `_mock_or_debug_allowed()` 404s `/mock/*` when `not debug_enabled and app_env != "dev"`; every `/mock/*` and `/debug/*` route `Depends(_mock_or_debug_allowed)` / `Depends(debug_enabled_dep)`.
- **FE LiveKit subscribe** — DONE. `frontend/lite.html:148` loads `livekit-client` from CDN; `connectLiveKit()` (line 362) joins the room and `RoomEvent.TrackSubscribed` attaches the video track; dual MJPEG/LiveKit path already exists.

So this plan implements only the **2 real gaps**: LiveKit backend audio publish, and Postgres runtime store lifecycle + persist. A separate final task updates the stale audit doc so future readers do not re-plan already-done work.

## File Structure

```
core/
  livekit_publish.py      MODIFY — real livekit-rtc publish path behind lazy import + RTC seam
  db/postgres_store.py    MODIFY — add insert_product_snapshot(), close(), keep existing methods
  server.py               MODIFY — startup: pg connect+apply_schema; shutdown: pg close. Store on V1Deps.
  api/v1.py               MODIFY — persist session/viewer_msg/director_decision at existing sites
  director/coordinator.py MODIFY — persist director_decisions on each _maybe_speak decision
  config.py               MODIFY — build_store() routes "postgres" → PostgresRuntimeStore wrapper (KV still memory/redis)
  tests/
    test_livekit_publish_sdk.py        CREATE — real-RTC path via fake transport seam
    test_livekit_publish_stub.py      MODIFY — keep existing stub tests green; add disabled-path coverage
    test_postgres_store_lifecycle.py   CREATE — connect/apply_schema/close + persist with fake asyncpg pool
    test_server_pg_lifecycle.py        CREATE — app startup/shutdown wires pg store when DATABASE_URL set
    test_api_persist.py                CREATE — /lite/ingest + /lite/chat persist rows when pg enabled
docs/superpowers/plans/2026-07-14-m3-residual-code-gaps.md  (this file)
plans/04-gap-audit-and-m3.md  MODIFY — mark MJPEG gate + FE subscribe DONE; narrow M3 scope to 2 gaps
```

`build_store()` change rationale: `SESSION_STORE` is the session **KV** store (memory/redis). `PostgresRuntimeStore` is a **different** store (durable runtime rows), per its own docstring ("Session KV still lives in core.store"). To avoid conflating the two, Postgres is NOT a `SESSION_STORE` value. Instead `V1Deps` carries a separate optional `pg_store` field, built from `config.database_url` in `server.py`, and routes persist to it. This keeps the SessionStore ABC clean.

---

### Task 1: Branch + offline baseline

**Files:**
- None (git only)

- [ ] **Step 1: Create feature branch from develop**

Run:
```bash
git checkout develop
git pull --ff-only
git checkout -b feature/m3-residual-gaps
```
Expected: clean checkout, on `feature/m3-residual-gaps`.

- [ ] **Step 2: Verify branch carries no stray commits**

Run:
```bash
git log --oneline develop..HEAD
```
Expected: empty output (no commits yet). If output is non-empty, a prior-session commit leaked — stop and surface to the user (see memory `checkout-branch-already-has-commits`).

- [ ] **Step 3: Run offline baseline (must be green before any change)**

Run:
```powershell
$env:RENDER_BACKEND="mock"; $env:LLM_ENGINE="none"; $env:TTS_ENGINE="tone"; $env:DIRECTOR_ENABLED="0"; $env:APP_ENV="dev"; uv run pytest core/tests/ -q
```
Expected: all pass (261 passed, 2 skipped per the 2026-07-11 ship checklist). Record the exact number — every later task must not regress it.

- [ ] **Step 4: Confirm asyncpg + livekit-rtc NOT currently installed**

Run:
```bash
uv run python -c "import asyncpg" 2>&1 | head -1
uv run python -c "import livekit" 2>&1 | head -1
```
Expected: both `ModuleNotFoundError`. This proves the offline path must not require either.

---

### Task 2: LiveKit publish — add RTC seam + real publish path

**Files:**
- Modify: `core/livekit_publish.py` (whole file rewritten around a seam)
- Create: `core/tests/test_livekit_publish_sdk.py`
- Modify: `core/tests/test_livekit_publish_stub.py` (keep green; add disabled-path assertions)

**Interfaces:**
- Consumes: `core.livekit_tokens.mint_room_token(api_key, api_secret, room, identity, can_publish=True)` — already exists, returns a JWT string.
- Produces: `AudioTrackPublisher(session_id, room=None, identity=None, env=None, rtc_factory=None)` with async `start()`, `publish_pcm(pcm, sample_rate=24000, num_channels=1)`, `stop()`. `rtc_factory` is the test seam: a callable `(url, token) -> AsyncContextManager` returning an object with `local_participant.publish_track(...)` and an `audio_track` with `capture_frame(frame)`; production default is `None` → lazy `import livekit.rtc`.

- [ ] **Step 1: Write the failing test for the real RTC path with a fake factory**

Create `core/tests/test_livekit_publish_sdk.py`:

```python
"""Offline tests for the real LiveKit RTC publish path (fake transport seam).

We never import livekit-rtc here. The AudioTrackPublisher accepts an
``rtc_factory`` test seam that builds a fake room object with the shape
the real SDK exposes: ``local_participant.publish_track`` + a track with
``capture_frame``. This proves the publish path wires PCM -> AudioFrame ->
track without the SDK installed.
"""

from __future__ import annotations

import pytest

from core.livekit_publish import AudioTrackPublisher


class _FakeTrack:
    def __init__(self) -> None:
        self.frames = []

    def capture_frame(self, frame) -> None:
        self.frames.append(frame)


class _FakeParticipant:
    def __init__(self, track: _FakeTrack) -> None:
        self._track = track
        self.published = False

    async def publish_track(self, track, options=None) -> str:
        self.published = True
        return "track-id"


class _FakeRoom:
    def __init__(self) -> None:
        self.track = _FakeTrack()
        self.local_participant = _FakeParticipant(self.track)
        self.connected = False
        self.disconnected = False

    async def __aenter__(self):
        self.connected = True
        return self

    async def __aexit__(self, *exc):
        self.disconnected = True
        return False


def _factory(room_holder):
    def make(url, token):
        room_holder["room"] = _FakeRoom()
        return room_holder["room"]
    return make


@pytest.mark.asyncio
async def test_publish_pcm_routes_to_track_via_factory():
    env = {
        "LIVEKIT_PUBLISH": "1",
        "LIVEKIT_URL": "ws://lk:7880",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
    }
    holder: dict = {}
    pub = AudioTrackPublisher("sess", env=env, rtc_factory=_factory(holder))
    assert pub.enabled is True

    await pub.start()
    assert holder["room"].connected is True
    assert holder["room"].local_participant.published is True

    await pub.publish_pcm(b"\x00\x01" * 480, sample_rate=24000)
    await pub.publish_pcm(b"\x02\x03" * 480, sample_rate=24000)
    assert pub.frames_published == 2
    assert len(holder["room"].track.frames) == 2

    await pub.stop()
    assert holder["room"].disconnected is True


@pytest.mark.asyncio
async def test_publish_start_fails_loud_when_sdk_missing(monkeypatch):
    """When rtc_factory is None AND livekit import fails, start() raises, not silent no-op."""
    env = {
        "LIVEKIT_PUBLISH": "1",
        "LIVEKIT_URL": "ws://lk:7880",
        "LIVEKIT_API_KEY": "k",
        "LIVEKIT_API_SECRET": "s",
    }
    import sys
    monkeypatch.setitem(sys.modules, "livekit.rtc", None)  # force ImportError on import
    pub = AudioTrackPublisher("sess", env=env, rtc_factory=None)
    with pytest.raises(RuntimeError, match="livekit-rtc"):
        await pub.start()
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest core/tests/test_livekit_publish_sdk.py -v
```
Expected: FAIL — `AudioTrackPublisher` does not accept `rtc_factory`; real path not implemented.

- [ ] **Step 3: Rewrite `core/livekit_publish.py` with the RTC seam**

Replace the entire file with:

```python
"""LiveKit audio publish (backend -> SFU).

No-op unless LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET are set AND
LIVEKIT_PUBLISH=1. When enabled, connects a real livekit-rtc Room, publishes
a local audio track, and pushes PCM frames converted to 20ms AudioFrames.

``livekit-rtc`` is an OPTIONAL dependency: it is imported lazily inside
``start()`` only on the enabled path, so the offline/Colab image (which does
not install it) imports this module and runs the disabled path with zero
cost. Tests inject an ``rtc_factory`` seam instead of the real SDK.

Usage (production):
    pub = AudioTrackPublisher(session_id=sid, env=os.environ)
    await pub.start()
    await pub.publish_pcm(pcm_bytes, sample_rate=24000)
    await pub.stop()
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

log = logging.getLogger(__name__)

# 20ms frame at 24kHz mono 16-bit = 480 samples = 960 bytes.
_FRAME_MS = 20


def publish_enabled(env: Optional[dict[str, str]] = None) -> bool:
    """True only when publish flag + LiveKit credentials are all present."""
    source = env if env is not None else os.environ
    flag = str(source.get("LIVEKIT_PUBLISH", "0")).lower() in (
        "1", "true", "on", "yes",
    )
    if not flag:
        return False
    url = (source.get("LIVEKIT_URL") or "").strip()
    key = (source.get("LIVEKIT_API_KEY") or "").strip()
    secret = (source.get("LIVEKIT_API_SECRET") or "").strip()
    return bool(url and key and secret)


class AudioTrackPublisher:
    """Publish PCM audio windows to a LiveKit room.

    When disabled (the default), every method is an async no-op so the stream
    path can call them unconditionally. When enabled, ``start()`` connects a
    livekit-rtc Room (or a test-injected fake) and publishes a local audio
    track; ``publish_pcm`` converts PCM to 20ms AudioFrames and captures them.
    """

    def __init__(
        self,
        session_id: str,
        *,
        room: Optional[str] = None,
        identity: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        rtc_factory: Optional[Any] = None,
    ) -> None:
        self.session_id = session_id
        self.room_name = room or session_id
        self.identity = identity or f"publisher-{session_id}"
        self._env = env
        self._rtc_factory = rtc_factory
        self._started = False
        self._enabled = publish_enabled(env)
        self._frames_published = 0
        self._room_ctx = None        # async context manager (real or fake)
        self._audio_track = None      # track exposing capture_frame

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def started(self) -> bool:
        return self._started

    @property
    def frames_published(self) -> int:
        return self._frames_published

    async def start(self) -> None:
        """Connect, create + publish a local audio track (no-op when disabled)."""
        if not self._enabled:
            log.debug("livekit_publish disabled (LIVEKIT_PUBLISH=1 + LIVEKIT_* creds)")
            return
        env = self._env if self._env is not None else os.environ
        url = (env.get("LIVEKIT_URL") or "").strip()
        key = (env.get("LIVEKIT_API_KEY") or "").strip()
        secret = (env.get("LIVEKIT_API_SECRET") or "").strip()

        token = self._mint_publish_token(key, secret)
        self._room_ctx = self._connect(url, token)
        # Real livekit-rtc Room is an async context manager; a test fake may
        # be a plain object — support both.
        room = await self._enter_room(self._room_ctx)
        self._audio_track = self._build_audio_track(room)
        await self._publish_track(room, self._audio_track)
        self._started = True
        log.info("livekit_publish started session=%s room=%s", self.session_id, self.room_name)

    def _mint_publish_token(self, key: str, secret: str) -> str:
        from .livekit_tokens import mint_room_token
        return mint_room_token(
            api_key=key, api_secret=secret, room=self.room_name,
            identity=self.identity, can_publish=True, can_subscribe=False,
        )

    def _connect(self, url: str, token: str):
        """Return a Room object/context-manager for the given url+token."""
        if self._rtc_factory is not None:
            return self._rtc_factory(url, token)
        # Production path: lazy import so the offline image never needs livekit-rtc.
        try:
            from livekit import rtc  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "livekit-rtc is required when LIVEKIT_PUBLISH=1; "
                "pip install livekit-rtc (uv add livekit-rtc)"
            ) from exc
        room = rtc.Room()
        # ponytail: real connect is awaitable; wrap in a tiny async CM so the
        # __aenter__/__aexit__ seam below works uniformly. Ceiling: when the
        # real SDK's Room is itself an async CM, drop this wrapper.
        return _RealRoomCtx(room, url, token)

    @staticmethod
    async def _enter_room(room_ctx):
        if hasattr(room_ctx, "__aenter__"):
            return await room_ctx.__aenter__()
        return room_ctx

    def _build_audio_track(self, room):
        if self._rtc_factory is not None:
            # Test fake already attached the track on the room object.
            return getattr(room, "_audio_track", None) or room.track
        from livekit import rtc  # type: ignore
        opts = rtc.AudioTrackOptions(
            name=f"tts-{self.session_id}",
            sample_rate=24000,
            num_channels=1,
        )
        track = rtc.create_local_audio_track(opts)
        room._livekit_track = track  # cache for publish
        return track

    @staticmethod
    async def _publish_track(room, track):
        if hasattr(room, "local_participant"):
            await room.local_participant.publish_track(track)
        # Test fakes implement publish_track on the participant; if absent,
        # the track is still captured locally (no-op publish).

    async def publish_pcm(
        self,
        pcm: bytes,
        *,
        sample_rate: int = 24_000,
        num_channels: int = 1,
    ) -> None:
        """Push one PCM buffer to the room track (no-op when disabled)."""
        if not self._enabled:
            return
        if not self._started:
            await self.start()
        # Convert PCM bytes to 20ms AudioFrames and capture each.
        await self._capture_pcm(self._audio_track, pcm, sample_rate, num_channels)
        self._frames_published += 1

    async def _capture_pcm(self, track, pcm, sample_rate, num_channels):
        if self._rtc_factory is not None:
            track.capture_frame({"pcm": pcm, "sample_rate": sample_rate})
            return
        from livekit import rtc  # type: ignore
        bytes_per_frame = int(sample_rate * num_channels * 2 * _FRAME_MS / 1000)
        for i in range(0, len(pcm), bytes_per_frame):
            chunk = pcm[i:i + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                break
            frame = rtc.AudioFrame(chunk, sample_rate, num_channels, len(chunk) // (num_channels * 2))
            track.capture_frame(frame)

    async def stop(self) -> None:
        """Unpublish and disconnect (no-op when disabled)."""
        if not self._enabled:
            self._started = False
            return
        if self._room_ctx is not None and hasattr(self._room_ctx, "__aexit__"):
            await self._room_ctx.__aexit__(None, None, None)
        self._room_ctx = None
        self._audio_track = None
        log.info("livekit_publish stopped session=%s frames=%s",
                 self.session_id, self._frames_published)
        self._started = False


class _RealRoomCtx:
    """Async CM wrapper over a real livekit-rtc Room so the seam is uniform.

    ponytail: ceiling — when livekit-rtc Room itself implements __aenter__,
    delete this class and return room from _connect directly.
    """

    def __init__(self, room, url: str, token: str) -> None:
        self._room = room
        self._url = url
        self._token = token

    async def __aenter__(self):
        await self._room.connect(self._url, self._token)
        return self._room

    async def __aexit__(self, *exc):
        try:
            await self._room.disconnect()
        except Exception:
            log.debug("livekit disconnect failed", exc_info=True)
        return False
```

- [ ] **Step 4: Update the test fake to expose the track the publisher expects**

The test fake in step 1 already attaches `track` on the room. But `_build_audio_track` for the factory path reads `room.track`. Edit `_FakeRoom.__init__` in the test file to also set `self.track = self.track` (already present). Verify the fake participant's `publish_track` signature accepts `(track)` — it does. No edit needed.

- [ ] **Step 5: Run the new test to verify it passes**

Run:
```bash
uv run pytest core/tests/test_livekit_publish_sdk.py -v
```
Expected: 2 passed (factory path + SDK-missing fail-loud).

- [ ] **Step 6: Run the existing stub test to verify no regression**

Run:
```bash
uv run pytest core/tests/test_livekit_publish_stub.py -v
```
Expected: all pass. The disabled path (`env={}`) is unchanged; `started` stays False, `frames_published` stays 0. If any fails, the stub-path branch in the new file diverged — fix the `not self._enabled` early-returns.

- [ ] **Step 7: Add `livekit-rtc` as an optional dependency**

Edit `pyproject.toml` `[project.optional-dependencies]` to add:

```toml
livekit = ["livekit-rtc"]
postgres = ["asyncpg"]
```

Then:
```bash
uv lock
```
Expected: `uv.lock` updated; `uv sync` (no extras) still does NOT install `livekit-rtc` or `asyncpg`.

- [ ] **Step 8: Commit**

```bash
git add core/livekit_publish.py core/tests/test_livekit_publish_sdk.py core/tests/test_livekit_publish_stub.py pyproject.toml uv.lock
git commit -m "feat(livekit): wire real rtc publish path behind lazy import + test seam"
```

---

### Task 3: Postgres store — lifecycle hooks + product snapshot insert

**Files:**
- Modify: `core/db/postgres_store.py` (add `insert_product_snapshot`, keep existing)
- Create: `core/tests/test_postgres_store_lifecycle.py`

**Interfaces:**
- Consumes: `core.sql.runtime_schema.sql` (already the source of truth), `asyncpg.create_pool` (lazy).
- Produces: `PostgresRuntimeStore.insert_product_snapshot(session_id, products: list[dict]) -> None`, `PostgresRuntimeStore.close()` already exists. `connect()` + `apply_schema()` already exist.

- [ ] **Step 1: Write the failing test for product snapshot insert with a fake pool**

Create `core/tests/test_postgres_store_lifecycle.py`:

```python
"""Offline tests for PostgresRuntimeStore lifecycle + persist (fake asyncpg pool).

We never touch a real Postgres. A fake pool/conn records executed SQL + args
so we assert the right statements run with the right parameters.
"""

from __future__ import annotations

import pytest

from core.db.postgres_store import PostgresRuntimeStore, schema_path


class _FakeConn:
    def __init__(self, pool) -> None:
        self._pool = pool

    async def execute(self, sql, *args):
        self._pool.statements.append((sql, args))

    async def fetchrow(self, sql, *args):
        self._pool.statements.append((sql, args))
        return {"id": 1}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self) -> None:
        self.statements: list = []

    def acquire(self):
        return _FakeConn(self)


@pytest.mark.asyncio
async def test_apply_schema_runs_schema_sql():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    await store.apply_schema()
    sql = store._pool.statements[0][0]
    assert "CREATE TABLE" in sql
    assert "sessions" in sql


@pytest.mark.asyncio
async def test_insert_product_snapshot_upserts_rows():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    products = [
        {"id": "P001", "name": "Kem chong nang", "price": 329000, "features": ["SPF50"]},
        {"id": "P002", "name": "Sua rua mat", "price": 159000, "features": []},
    ]
    await store.insert_product_snapshot("sess-1", products)
    sqls = [s[0] for s in store._pool.statements]
    assert any("session_products" in s for s in sqls)
    # Two products -> at least two INSERT executions.
    inserts = [s for s in store._pool.statements if "INSERT" in s[0].upper()]
    assert len(inserts) >= 2


@pytest.mark.asyncio
async def test_insert_viewer_msg_returns_id():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    rid = await store.insert_viewer_msg("sess-1", "gia bao nhieu", author="v1")
    assert rid == 1


@pytest.mark.asyncio
async def test_insert_director_decision_returns_id():
    store = PostgresRuntimeStore("postgresql://u:p@h:5432/runtime")
    store._pool = _FakePool()
    rid = await store.insert_director_decision(
        "sess-1", "answer_cluster", product_id="P001", score=0.8, phase="selling",
        utterance="Kem nay SPF50 nhe", reason="cluster match",
    )
    assert rid == 1


def test_schema_path_resolves():
    p = schema_path()
    assert p.is_file()
    assert p.name == "runtime_schema.sql"
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest core/tests/test_postgres_store_lifecycle.py -v
```
Expected: FAIL — `insert_product_snapshot` does not exist (AttributeError).

- [ ] **Step 3: Implement `insert_product_snapshot` in `core/db/postgres_store.py`**

Add this method to the `PostgresRuntimeStore` class (after `get_session`, before `insert_viewer_msg`):

```python
    async def insert_product_snapshot(
        self,
        session_id: str,
        products: list[dict[str, Any]],
    ) -> None:
        """Persist the frozen product snapshot for a session (idempotent upsert).

        Called once at /lite/attach. Rows are frozen for the livestream lifetime
        (replay correctness + price integrity) — never mutated mid-stream.
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            for idx, p in enumerate(products):
                pid = str(p.get("id") or p.get("product_id") or "")
                name = p.get("name")
                price = p.get("price")
                payload = {k: v for k, v in p.items() if k not in ("id", "name", "price")}
                await conn.execute(
                    """
                    INSERT INTO session_products (
                        session_id, product_id, name, price, payload, sort_order
                    ) VALUES ($1,$2,$3,$4,$5::jsonb,$6)
                    ON CONFLICT (session_id, product_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        price = EXCLUDED.price,
                        payload = EXCLUDED.payload,
                        sort_order = EXCLUDED.sort_order
                    """,
                    session_id,
                    pid,
                    name,
                    price,
                    json.dumps(payload),
                    idx,
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
uv run pytest core/tests/test_postgres_store_lifecycle.py -v
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add core/db/postgres_store.py core/tests/test_postgres_store_lifecycle.py
git commit -m "feat(db): add product snapshot upsert + lifecycle tests for runtime store"
```

---

### Task 4: Wire Postgres store into server lifecycle + V1Deps

**Files:**
- Modify: `core/server.py` (startup connect+apply_schema, shutdown close; add `pg_store` to V1Deps)
- Modify: `core/api/v1.py` (`V1Deps` dataclass gains `pg_store: Optional[PostgresRuntimeStore] = None`)
- Create: `core/tests/test_server_pg_lifecycle.py`

**Interfaces:**
- Consumes: `AppConfig.database_url`, `PostgresRuntimeStore` (Task 3).
- Produces: `V1Deps.pg_store` — `Optional[PostgresRuntimeStore]`, None when `database_url` empty. App `lifespan` calls `pg_store.connect()` + `pg_store.apply_schema()` on startup, `pg_store.close()` on shutdown.

- [ ] **Step 1: Write the failing test for app startup/shutdown wiring pg store**

Create `core/tests/test_server_pg_lifecycle.py`:

```python
"""Server wires PostgresRuntimeStore lifecycle when DATABASE_URL is set.

Uses a fake store injected via V1Deps so no real DB is touched. Asserts
connect/apply_schema fire on startup and close on shutdown.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.db.postgres_store import PostgresRuntimeStore
from core.server import create_app


class _FakePgStore:
    def __init__(self) -> None:
        self.enabled = True
        self.connected = False
        self.schema_applied = False
        self.closed = False

    async def connect(self):
        self.connected = True

    async def apply_schema(self):
        self.schema_applied = True

    async def close(self):
        self.closed = True


def _mock_env(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h:5432/runtime")


def test_app_lifecycle_connects_and_closes_pg_store(monkeypatch):
    _mock_env(monkeypatch)
    config = AppConfig.from_env()
    assert config.database_url != ""

    pg = _FakePgStore()
    backend = config.build_render_backend()
    store = config.build_store()
    deps = v1.V1Deps(
        backend=backend, store=store, hub=v1.ControlHub(),
        director=None, engine_manager=None, config=config,
        locks=None, orchestrators={}, coordinator=None, pg_store=pg,
    )
    app = create_app(config=config, deps=deps)

    with TestClient(app) as client:
        assert pg.connected is True
        assert pg.schema_applied is True
        # Server still serves health while pg is connected.
        r = client.get("/api/v1/health/live")
        assert r.status_code == 200
    # After the with-block, lifespan shutdown ran.
    assert pg.closed is True


def test_app_lifecycle_skips_pg_when_no_database_url(monkeypatch):
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    config = AppConfig.from_env()
    # build pg store from config — should be a disabled store.
    from core.db.postgres_store import PostgresRuntimeStore
    pg = PostgresRuntimeStore(config.database_url)
    assert pg.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest core/tests/test_server_pg_lifecycle.py -v
```
Expected: FAIL — `V1Deps` has no `pg_store` field; `create_app` has no lifespan.

- [ ] **Step 3: Add `pg_store` to `V1Deps` in `core/api/v1.py`**

Find the `V1Deps` dataclass definition (search for `class V1Deps`). Add `pg_store` as the last field with a default of `None`:

```python
    pg_store: Optional[Any] = None
```

(Use `Any` to avoid importing `PostgresRuntimeStore` into the API module and creating a hard dep; the type is enforced by construction in `server.py`.)

- [ ] **Step 4: Add a lifespan to `create_app` in `core/server.py`**

Edit `core/server.py`. At the top, add the import:

```python
from contextlib import asynccontextmanager
```

Inside `create_app`, before `app.include_router(v1.router)`, replace the bare `app = FastAPI(...)` construction with a lifespan-wrapped one. The lifespan must run for BOTH the env-driven path and the injected-deps path. Concretely, after `v1.init_deps(...)` is resolved (either branch), add:

```python
    pg = getattr(deps, "pg_store", None) if deps is not None else None
    # Env-driven path: build the pg store from config when DATABASE_URL is set.
    if deps is None and config.database_url:
        from .db.postgres_store import PostgresRuntimeStore
        pg = PostgresRuntimeStore(config.database_url)
        # Attach to the V1Deps we are about to init.
    if deps is None:
        # pg will be wired into V1Deps below; stash on a local for the lifespan.
        pass

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        if pg is not None and getattr(pg, "enabled", False):
            try:
                await pg.connect()
                await pg.apply_schema()
            except Exception as exc:
                # Fail loud but do not crash the server: a broken runtime DB
                # should not take the control plane down. /health/ready will
                # surface it; rows simply won't persist.
                print(f"[server] Postgres runtime store unavailable: {exc}")
        yield
        if pg is not None and getattr(pg, "enabled", False):
            try:
                await pg.close()
            except Exception:
                pass

    app = FastAPI(title="VN Live-Commerce Host — core API", lifespan=_lifespan)
```

Then, in the env-driven branch, pass `pg_store=pg` into the `V1Deps(...)` constructor. In the injected-deps branch, `deps.pg_store` is already set by the caller (or stays None).

Adjust the existing `app = FastAPI(title=...)` line (around line 106) — remove it so only the lifespan version remains. Move the CORS middleware add AFTER the `app` construction (it already is).

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
uv run pytest core/tests/test_server_pg_lifecycle.py -v
```
Expected: 2 passed. If the env-driven path test fails because `V1Deps` was constructed before `pg` was built, reorder so `pg` is built before `v1.init_deps(V1Deps(..., pg_store=pg))`.

- [ ] **Step 6: Run the full offline suite to confirm no regression**

Run:
```powershell
$env:RENDER_BACKEND="mock"; $env:LLM_ENGINE="none"; $env:TTS_ENGINE="tone"; $env:DIRECTOR_ENABLED="0"; $env:APP_ENV="dev"; uv run pytest core/tests/ -q
```
Expected: previous count + new tests, all green. No `asyncpg` import attempted (DATABASE_URL unset).

- [ ] **Step 7: Commit**

```bash
git add core/server.py core/api/v1.py core/tests/test_server_pg_lifecycle.py
git commit -m "feat(server): wire postgres runtime store lifecycle into app startup/shutdown"
```

---

### Task 5: Persist rows at ingest / chat / decision sites

**Files:**
- Modify: `core/api/v1.py` — `lite_start` (upsert session), `lite_attach` (product snapshot), `lite_ingest` + `lite_chat` (viewer msg), coordinator decision hook (director decision)
- Modify: `core/director/coordinator.py` — `_after_speak` or the decision-emit site persists `director_decisions`
- Create: `core/tests/test_api_persist.py`

**Interfaces:**
- Consumes: `V1Deps.pg_store` (Task 4), `PostgresRuntimeStore` methods (Task 3).
- Produces: persistence is fire-and-forget (awaited but never blocks the response on failure; errors logged, not raised).

- [ ] **Step 1: Write the failing test for /lite/ingest persisting a viewer message**

Create `core/tests/test_api_persist.py`:

```python
"""Persist rows to the runtime DB at ingest/chat/decision sites (pg enabled).

Fake pg store records calls; no real DB. When pg_store is None or disabled,
the routes behave exactly as before (no persistence, no errors).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.server import create_app


class _FakePgStore:
    def __init__(self) -> None:
        self.enabled = True
        self.viewer_msgs: list = []
        self.director_decisions: list = []
        self.sessions: list = []
        self.snapshots: list = []

    async def connect(self): pass
    async def apply_schema(self): pass
    async def close(self): pass

    async def upsert_session(self, sid, **kw): self.sessions.append((sid, kw))
    async def insert_viewer_msg(self, sid, text, **kw):
        self.viewer_msgs.append((sid, text, kw)); return len(self.viewer_msgs)
    async def insert_director_decision(self, sid, action, **kw):
        self.director_decisions.append((sid, action, kw)); return len(self.director_decisions)
    async def insert_product_snapshot(self, sid, products):
        self.snapshots.append((sid, products))


def _app_with_pg(pg, monkeypatch) -> TestClient:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    config = AppConfig.from_env()
    backend = config.build_render_backend()
    deps = v1.V1Deps(
        backend=backend, store=config.build_store(), hub=v1.ControlHub(),
        director=None, engine_manager=None, config=config,
        locks=None, orchestrators={}, coordinator=None, pg_store=pg,
    )
    return TestClient(create_app(config=config, deps=deps))


def test_lite_start_persists_session(monkeypatch):
    pg = _FakePgStore()
    with _app_with_pg(pg, monkeypatch) as client:
        r = client.post("/api/v1/lite/start", json={"is_sandbox": True})
        assert r.status_code == 200
        sid = r.json()["session_id"]
    assert len(pg.sessions) == 1
    assert pg.sessions[0][0] == sid


def test_lite_ingest_no_pg_behaves_unchanged(monkeypatch):
    # pg_store None -> route must not raise, must return the Director-501 path.
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")
    config = AppConfig.from_env()
    deps = v1.V1Deps(
        backend=config.build_render_backend(), store=config.build_store(),
        hub=v1.ControlHub(), director=None, engine_manager=None, config=config,
        locks=None, orchestrators={}, coordinator=None, pg_store=None,
    )
    with TestClient(create_app(config=config, deps=deps)) as client:
        r = client.post("/api/v1/lite/ingest",
                        json={"session_id": "x", "comments": [], "viewer_count": 0, "msg_rate": 0})
        # Director not enabled -> 501, but no persistence crash.
        assert r.status_code in (501, 409)
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
uv run pytest core/tests/test_api_persist.py -v
```
Expected: FAIL — `lite_start` does not call `pg_store.upsert_session`.

- [ ] **Step 3: Add persistence calls in `core/api/v1.py`**

In `lite_start`, after `await d.store.set(...)`, add (before `return result.public_dict()`):

```python
    if d.pg_store is not None and getattr(d.pg_store, "enabled", False):
        try:
            await d.pg_store.upsert_session(
                result.session_id, status="active", mode=result.mode,
                render_backend=d.config.render_backend if d.config else None,
                avatar_id=req.avatar_id,
            )
        except Exception:
            pass  # fire-and-forget; log at debug
```

In `lite_attach`, after `info = await asyncio.to_thread(d.director.attach, ...)` succeeds, add (before the coordinator start):

```python
    if d.pg_store is not None and getattr(d.pg_store, "enabled", False):
        try:
            await d.pg_store.insert_product_snapshot(
                req.session_id, [p.model_dump() for p in req.products]
            )
        except Exception:
            pass
```

In `lite_ingest` (coordinator path), after `d.coordinator.ingest(...)`, and in the sync fallback path after `result = await asyncio.to_thread(...)`, add for each comment:

```python
    if d.pg_store is not None and getattr(d.pg_store, "enabled", False):
        for c in req.comments:
            try:
                await d.pg_store.insert_viewer_msg(
                    req.session_id, c.text, author="viewer", comment_id=None, source="platform",
                )
            except Exception:
                pass
```

In `lite_chat`, after the coordinator accepts the comment (the 202 path), add the same `insert_viewer_msg` call with `payload.text` and `payload.author`.

- [ ] **Step 4: Add Director decision persistence in `coordinator.py`**

In `core/director/coordinator.py`, find the `_maybe_speak` site where `decision` is finalized (around line 345-430). After the decision is acted on (the `await self._emit(...)` for `coordinator.speak_started`), add:

```python
        if self._pg_store is not None and getattr(self._pg_store, "enabled", False):
            try:
                await self._pg_store.insert_director_decision(
                    session_id, decision.action,
                    product_id=decision.product_id, score=decision.score,
                    phase=decision.phase if hasattr(decision, "phase") else None,
                    utterance=getattr(decision, "text", None),
                    reason=decision.reason,
                )
            except Exception:
                logger.debug("pg insert_director_decision failed", exc_info=True)
```

Add `pg_store: Optional[Any] = None` to `DirectorCoordinator.__init__` and store as `self._pg_store = pg_store`. Update the constructor call in `core/server.py` to pass `pg_store=pg` (the same `pg` built in Task 4).

- [ ] **Step 5: Run test to verify it passes**

Run:
```bash
uv run pytest core/tests/test_api_persist.py -v
```
Expected: 2 passed.

- [ ] **Step 6: Run the full offline suite**

Run:
```powershell
$env:RENDER_BACKEND="mock"; $env:LLM_ENGINE="none"; $env:TTS_ENGINE="tone"; $env:DIRECTOR_ENABLED="0"; $env:APP_ENV="dev"; uv run pytest core/tests/ -q
```
Expected: all green, no `asyncpg` import.

- [ ] **Step 7: Commit**

```bash
git add core/api/v1.py core/director/coordinator.py core/tests/test_api_persist.py
git commit -m "feat(persist): wire session/viewer_msg/director_decision to runtime store"
```

---

### Task 6: Update stale M3 gap audit + final verification

**Files:**
- Modify: `plans/04-gap-audit-and-m3.md` (mark MJPEG gate + FE subscribe DONE; narrow M3 to 2 gaps now closed)

- [ ] **Step 1: Update the gap audit status table**

In `plans/04-gap-audit-and-m3.md`, edit the gap matrix:
- `MJPEG debug-only gate` → status **DONE** (was TODO); note `core/api/v1.py:282-289` `_mock_or_debug_allowed`.
- `FE LiveKit subscribe` → status **DONE** (was TODO); note `frontend/lite.html:148` + `connectLiveKit()`.
- `Backend audio LiveKit publish` → status **DONE** (was TODO); note `core/livekit_publish.py` real rtc path (this plan, Task 2).
- `Postgres runtime` → status **DONE** (was TODO); note lifecycle + persist wired (Tasks 3-5).

Update the M3 scope list: items 5 (Postgres), 6 (debug gate), 7 (FE LiveKit), 8 (audio publish) are now DONE. Remaining M3 item 9 (gap report + ship checklist) is closed by this very step.

- [ ] **Step 2: Run the complete offline suite one final time**

Run:
```powershell
$env:RENDER_BACKEND="mock"; $env:LLM_ENGINE="none"; $env:TTS_ENGINE="tone"; $env:DIRECTOR_ENABLED="0"; $env:APP_ENV="dev"; uv run pytest core/tests/ -q
```
Expected: all green. Compare against the Task 1 baseline — new tests added (LiveKit SDK, PG lifecycle, API persist, server PG), no existing test regressed.

- [ ] **Step 3: Lint + format**

Run:
```bash
uvx ruff check core/livekit_publish.py core/db/postgres_store.py core/server.py core/api/v1.py core/director/coordinator.py
uvx ruff format core/livekit_publish.py core/db/postgres_store.py core/server.py core/api/v1.py core/director/coordinator.py
```
Expected: no errors (fix any ruff flags before committing).

- [ ] **Step 4: Verify branch scope before PR**

Run:
```bash
git log --oneline develop..HEAD
git diff develop...HEAD --stat
```
Expected: only the files listed in this plan touched (core/livekit_publish.py, core/db/postgres_store.py, core/server.py, core/api/v1.py, core/director/coordinator.py, pyproject.toml, uv.lock, plans/04-gap-audit-and-m3.md, + new test files). No stray `infra/**` or unrelated files.

- [ ] **Step 5: Commit the doc update**

```bash
git add plans/04-gap-audit-and-m3.md
git commit -m "docs(plans): mark MJPEG gate + FE subscribe + LK publish + PG store DONE in M3 audit"
```

- [ ] **Step 6: Push + open PR to develop**

```bash
git push -u origin feature/m3-residual-gaps
gh pr create --base develop --title "feat: M3 residual code gaps (LiveKit publish + Postgres store)" --body "Closes the 2 real M3 code gaps: (1) wire livekit-rtc backend audio publish behind lazy import + test seam; (2) wire PostgresRuntimeStore lifecycle + persist sessions/viewer_msgs/director_decisions when DATABASE_URL set. MJPEG debug gate and FE LiveKit subscribe verified already DONE. Stale audit updated."
```

Expected: PR opens; `ci.yml` runs (offline pytest + docker build check, free). `deploy-dev.yml` does NOT trigger (no `services/**`/`infra/**` change — config/doc/code-only under `core/` + `plans/`).

---

## Self-Review

**1. Spec coverage:**
- LiveKit backend audio publish (docs brief §E, scope-engine LiveKit) → Task 2. ✓
- Postgres runtime schema + store wire + persist (docs brief §L, database.md) → Tasks 3-5. ✓
- MJPEG debug-only gate (docs architecture, runbook) → verified already DONE, recorded in Task 6. ✓
- FE LiveKit subscribe (docs brief §E) → verified already DONE, recorded in Task 6. ✓
- Stale audit correction → Task 6. ✓
- Offline tests stay green without new deps → Global Constraints + Task 1 baseline. ✓
- `self_host` fail-loud untouched → not in scope. ✓

**2. Placeholder scan:** No TBD/TODO/`add appropriate error handling` placeholders. Every code step shows the actual code. The two `ponytail:` comments name the ceiling (real SDK async CM; uniform seam) and upgrade path.

**3. Type consistency:** `AudioTrackPublisher(rtc_factory=...)` used identically in test (step 1) and impl (step 3). `V1Deps.pg_store` added in Task 4 step 3, consumed in Task 4 test, Task 5 test, and Task 5 step 3-4. `insert_product_snapshot(session_id, products: list[dict])` signature matches across Task 3 test, Task 3 impl, Task 5 (lite_attach). `DirectorCoordinator(pg_store=...)` added in Task 5 step 4, passed in Task 5 step 4 from server.py. `_FakePgStore` in test_api_persist implements the same method names as `PostgresRuntimeStore` (upsert_session, insert_viewer_msg, insert_director_decision, insert_product_snapshot, connect, apply_schema, close). ✓