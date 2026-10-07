import asyncio
import time

import pytest
from fastapi import HTTPException

import backend.application.clients.avatar.lemonslice as mod
from backend.api.v1.sessions import _stop_cancelled_session
from backend.application.terminal_outcomes import TerminalOutcomes
from backend.application.render.engines_base import StartOptions
from backend.application.clients.avatar.lemonslice import LemonSliceError
from .test_lemonslice_backend import make
from .test_terminal_outcomes import FakePg, hot, stop_container


def test_failed_provider_terminate_is_explicit_and_retryable_after_recovery():
    backend, room, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    rest.control_status = 503
    try:
        with pytest.raises(LemonSliceError) as caught:
            backend.stop(sid)
        assert caught.value.code == "provider_cleanup_incomplete"
        assert room.disconnected and sid not in backend._sessions
        count = len(rest.controls)
        assert count == 2
        rest.control_status = 200
        backend.stop(sid)
        assert len(rest.controls) == count + 1
        assert all(event == "terminate" for _, event in rest.controls)
        assert all("ls-1" in url for url, _ in rest.controls)
        backend.stop(sid)
        assert len(rest.controls) == count + 1
        assert len(rest.starts) == 1
    finally:
        rest.control_status = 200
        backend.stop_all()


@pytest.mark.parametrize("status", [404, 410])
def test_gone_provider_is_confirmed_cleanup(status):
    backend, _, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    rest.control_status = status
    try:
        backend.stop(sid)
        assert backend._pending_terminate == {}
        assert len(rest.controls) == 1
    finally:
        backend.stop_all()


def test_unresolved_cleanup_refuses_new_paid_creation_at_capacity(monkeypatch):
    monkeypatch.setattr(mod, "_MAX_OWNED_SESSIONS", 1)
    backend, _, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    rest.control_status = 503
    try:
        with pytest.raises(mod.ProviderCleanupIncomplete):
            backend.stop(sid)
        with pytest.raises(LemonSliceError) as caught:
            backend.start(StartOptions())
        assert caught.value.code == "cleanup_capacity"
        assert len(rest.starts) == 1
        assert backend._pending_terminate == {sid: "ls-1"}
    finally:
        rest.control_status = 200
        backend.stop_all()


@pytest.mark.parametrize("recover", [True, False])
async def test_provider_failure_reaches_terminal_retry_and_truthful_cleanup(recover):
    backend, room, rest = make(keepalive_s=0)
    sid = backend.start(StartOptions()).session_id
    d = stop_container([])
    d.backend = backend
    pg = FakePg()
    d.terminal_outcomes = TerminalOutcomes(pg)
    meta = hot("ending")
    meta["execution_contract"]["runtime_session_id"] = sid
    await d.store.set(sid, meta)
    rest.control_status = 503
    try:
        for _ in range(1 if recover else 2):
            with pytest.raises(HTTPException) as caught:
                await _stop_cancelled_session(d, sid)
            assert caught.value.status_code == 503
            assert caught.value.detail["code"] == "terminal_cleanup_retry"
            assert await d.store.get(sid) is not None and not pg.stored
            assert room.disconnected
        if recover:
            rest.control_status = 200
        await _stop_cancelled_session(d, sid)
        (record,) = pg.stored.values()
        assert record.cleanup.status == ("succeeded" if recover else "failed")
        assert record.cleanup.attempts == (2 if recover else 3)
        assert bool(backend._pending_terminate) is (not recover)
        assert await d.store.get(sid) is None
        assert len(rest.starts) == 1
    finally:
        rest.control_status = 200
        backend.stop_all()


def test_control_timeout_is_bounded_and_keeps_same_provider_for_retry():
    backend, room, rest = make(keepalive_s=0, request_timeout_s=0.05)
    sid = backend.start(StartOptions()).session_id

    async def stalled(*args):
        await asyncio.sleep(10)

    backend._async_post = stalled
    try:
        started = time.monotonic()
        with pytest.raises(mod.ProviderCleanupIncomplete):
            backend.stop(sid)
        assert time.monotonic() - started < 2
        assert room.disconnected and backend._pending_terminate == {sid: "ls-1"}
        backend._async_post = None
        backend.stop(sid)
        assert backend._pending_terminate == {} and len(rest.starts) == 1
    finally:
        backend._async_post = None
        backend.stop_all()


def test_start_failure_keeps_cleanup_identity_for_shutdown_retry():
    backend, room, rest = make(mode="never", keepalive_s=0)
    rest.control_status = 503
    try:
        with pytest.raises(mod.ProviderCleanupIncomplete):
            backend.start(StartOptions())
        assert room.disconnected and not backend._sessions
        assert list(backend._pending_terminate.values()) == ["ls-1"]
        assert len(rest.starts) == 1
        rest.control_status = 200
        backend.stop_all()
        assert not backend._pending_terminate and len(rest.controls) == 3
    finally:
        rest.control_status = 200
        backend.stop_all()


def test_failed_late_creation_cleanup_keeps_runtime_identity_for_retry():
    backend, _, rest = make(keepalive_s=0)
    rest.control_status = 503

    async def late():
        completed = asyncio.get_running_loop().create_future()
        completed.set_result("ls-1")
        await backend._reap_late_session(completed, "late-runtime")

    try:
        backend._run(late())
        assert backend._pending_terminate == {"late-runtime": "ls-1"}
        rest.control_status = 200
        backend.stop("late-runtime")
        assert not backend._pending_terminate and len(rest.controls) == 3
        assert not rest.starts
    finally:
        rest.control_status = 200
        backend.stop_all()
