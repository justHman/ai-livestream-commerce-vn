from types import SimpleNamespace

import pytest

from backend.application.avatar_lifecycle import AvatarLifecycle
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.render.engines_base import StartOptions
from .test_lemonslice_backend import make, win


class Speech:
    def __init__(self):
        self.canceled = []
        self.blocked = []

    def cancel(self, sid):
        self.canceled.append(sid)

    def block(self, sid, reason):
        self.blocked.append((sid, reason))


def container(backend):
    return SimpleNamespace(
        backend=backend,
        store=InMemorySessionStore(),
        approved_speech=Speech(),
        coordinator=None,
        director=None,
        orchestrators={},
        livekit_publishers=None,
        terminal_outcomes=None,
        usage_sender=None,
        hub=None,
        locks=SimpleNamespace(drop=lambda _: None),
    )


@pytest.mark.asyncio
async def test_abandoned_ready_session_ttl_releases_avatar_and_runtime_metadata():
    backend, room, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    d = container(backend)
    await d.store.set(sid, {"status": "active"})
    service = AvatarLifecycle(d, ready_ttl_s=1800)
    backend._sessions[sid].created_at -= 1801
    try:
        await service.check(sid)
        assert not backend.session_ids() and room.disconnected
        assert any(event == "terminate" for _, event in rest.controls)
        assert await d.store.get(sid) is None
        assert d.approved_speech.canceled
    finally:
        backend.stop_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["warming", "selling", "closing"])
async def test_ttl_never_stops_on_air(phase):
    backend, _, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    d = container(backend)
    await d.store.set(sid, {"execution_contract": {"phase": phase}})
    backend._sessions[sid].created_at -= 1801
    try:
        await AvatarLifecycle(d).check(sid)
        assert sid in backend.session_ids() and rest.controls == []
    finally:
        backend.stop_all()


@pytest.mark.asyncio
async def test_zero_ttl_disables_expiry_and_recent_speech_prevents_expiry():
    backend, _, _ = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    d = container(backend)
    await d.store.set(sid, {"status": "active"})
    backend._sessions[sid].created_at -= 1801
    try:
        await AvatarLifecycle(d, ready_ttl_s=0).check(sid)
        assert sid in backend.session_ids()
        backend.stream_audio(sid, win("u", 0, final=True))
        backend._sessions[sid].last_stream_at -= 121
        await AvatarLifecycle(d).check(sid)
        assert sid in backend.session_ids()
    finally:
        backend.stop_all()


@pytest.mark.asyncio
async def test_p0_ttl_persists_terminal_and_retries_failed_provider_cleanup():
    from backend.application.execution_contract import ExecutionState
    from backend.application.terminal_outcomes import TerminalOutcomes
    from fastapi import HTTPException
    from .test_terminal_outcomes import FakePg

    backend, room, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    d = container(backend)
    pg = FakePg()
    d.terminal_outcomes = TerminalOutcomes(pg)
    raw = ExecutionState(
        tenant_id="tenant",
        business_session_id="business",
        runtime_session_id=sid,
        generation="g1",
        phase="ready",
        runtime_ready=True,
    )
    meta = {"execution_contract": raw.model_dump(mode="json")}
    await d.store.set(sid, meta)
    await d.terminal_outcomes.register(sid, meta)
    backend._sessions[sid].created_at -= 1801
    service = AvatarLifecycle(d)
    try:
        rest.control_status = 503
        with pytest.raises(HTTPException) as error:
            await service.check(sid)
        assert error.value.detail["code"] == "terminal_cleanup_retry"
        assert room.disconnected
        stored = await d.store.get(sid)
        assert stored["execution_contract"]["phase"] == "failed"
        assert service.pending[sid] == "RUNTIME_READY_TTL_EXPIRED"
        rest.control_status = 200
        await service.check(sid)
        assert await d.store.get(sid) is None
        assert len(pg.stored) == 1 and not service.pending
    finally:
        rest.control_status = 200
        backend.stop_all()
