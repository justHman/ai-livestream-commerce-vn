import asyncio
from types import SimpleNamespace

import pytest

from backend.application import budget_lease
from backend.application.clients.avatar.lemonslice import LemonSliceRenderBackend


async def test_pending_discovery_continues_past_one_unreadable_session():
    class Store:
        async def list_session_ids(self):
            return ["broken", "good"]

        async def get(self, sid):
            if sid == "broken":
                raise ConnectionError("store unavailable for one session")
            return {budget_lease.TERMINATION_KEY: "pending"}

    watcher = budget_lease.BudgetLeaseEnforcer(
        SimpleNamespace(store=Store(), approved_speech=SimpleNamespace()),
        budget_lease.LeaseSettings(enabled=True),
    )
    await watcher._scan_pending()
    assert "good" in watcher.tracked


def test_lemonslice_stop_all_closes_client_and_loop_even_when_drain_fails(monkeypatch):
    backend = LemonSliceRenderBackend.__new__(LemonSliceRenderBackend)
    backend._loop = object()
    backend._sessions = {}
    backend._closing = {}
    closed = []

    async def drain(kind):
        raise RuntimeError("failed drain")

    async def close():
        closed.append("client")

    monkeypatch.setattr(backend, "_drain", drain)
    monkeypatch.setattr(backend, "_run", lambda coro: asyncio.run(coro))
    monkeypatch.setattr(backend, "_close_client", close)
    monkeypatch.setattr(backend, "_shutdown_loop", lambda: closed.append("loop"))
    with pytest.raises(RuntimeError, match="failed drain"):
        backend.stop_all()
    assert closed == ["client", "loop"]
