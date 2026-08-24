"""Shared-quota rate limiting: Layer-1 logical quota + authenticated identity keys.

C.2 / R8.10: the old limiter was process-local and keyed by client IP first —
quota multiplied across replicas and authenticated users behind NAT shared one
IP bucket. These tests pin the two-layer model:

  - ``SharedQuotaLimiter`` over ONE store = one logical quota across replicas.
  - ``quota_identity_key`` keys REST quotas by authenticated identity, under an
    explicit trusted-proxy policy for the IP fallback.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from backend.application.rate_limit import (
    InMemoryRateLimitStore,
    RedisRateLimitStore,
    SharedQuotaLimiter,
    quota_identity_key,
)
from backend.api.v1.router import rate_limit_viewer


def _http_request(
    client_host: str = "203.0.113.9",
    headers: list[tuple[bytes, bytes]] | None = None,
):
    from starlette.requests import Request

    return Request(
        scope={
            "type": "http",
            "client": (client_host, 1234),
            "headers": headers or [],
        }
    )


@pytest.mark.asyncio
async def test_two_replica_instances_share_one_logical_quota() -> None:
    store = InMemoryRateLimitStore()
    limiter_a = SharedQuotaLimiter(store, requests_limit=3, window_seconds=60.0)
    limiter_b = SharedQuotaLimiter(store, requests_limit=3, window_seconds=60.0)

    allowed = [
        await limiter_a.allow("viewer:quota"),
        await limiter_a.allow("viewer:quota"),
        await limiter_b.allow("viewer:quota"),
        await limiter_b.allow("viewer:quota"),
    ]

    assert allowed == [True, True, True, False]
    assert sum(allowed) == 3


class _FakeRedis:
    """Minimal async redis stand-in for the fixed-window Lua counter.

    One instance == one Redis server: ``eval`` runs the INCR + PEXPIRE script
    against an in-process dict. Expiry is recorded (PEXPIRE) so a test can
    simulate a window reset by mutating the fake state — no real sleeps.
    """

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._expire_ms: dict[str, int] = {}
        self.scripts_seen: list[str] = []

    async def eval(self, script: str, numkeys: int, *args) -> int:
        self.scripts_seen.append(script)
        key = args[0]
        count = self._counts.get(key, 0) + 1
        self._counts[key] = count
        if count == 1:
            self._expire_ms[key] = int(args[1])
        return count


@pytest.mark.asyncio
async def test_redis_store_counts_across_instances_with_fake_client() -> None:
    fake = _FakeRedis()
    store_a = RedisRateLimitStore(client=fake)
    store_b = RedisRateLimitStore(client=fake)

    allowed = [
        await store_a.allow("viewer:quota", limit=2, window_seconds=60.0),
        await store_b.allow("viewer:quota", limit=2, window_seconds=60.0),
        await store_b.allow("viewer:quota", limit=2, window_seconds=60.0),
    ]

    assert allowed == [True, True, False]
    assert "INCR" in fake.scripts_seen[0]
    assert "PEXPIRE" in fake.scripts_seen[0]


def test_authenticated_identity_beats_shared_nat_ip() -> None:
    cfg = SimpleNamespace(
        admin_api_token="adm",
        backend_api_token="view",
        trusted_proxy_client_ip=False,
    )

    assert (
        quota_identity_key(
            _http_request(headers=[(b"authorization", b"Bearer view")]), cfg
        )
        == "id:viewer"
    )
    assert (
        quota_identity_key(
            _http_request(headers=[(b"authorization", b"Bearer adm")]), cfg
        )
        == "id:admin"
    )
    assert (
        quota_identity_key(
            _http_request(headers=[(b"authorization", b"Bearer nope")]), cfg
        )
        == "ip:203.0.113.9"
    )


def test_trusted_proxy_policy_explicit() -> None:
    req = _http_request(headers=[(b"x-forwarded-for", b"198.51.100.7, 10.0.0.1")])
    cfg_off = SimpleNamespace(
        admin_api_token="", backend_api_token="", trusted_proxy_client_ip=False
    )
    cfg_on = SimpleNamespace(
        admin_api_token="", backend_api_token="", trusted_proxy_client_ip=True
    )

    assert quota_identity_key(req, cfg_off) == "ip:203.0.113.9"
    assert quota_identity_key(req, cfg_on) == "ip:198.51.100.7"


def test_router_deps_use_identity_and_await_shared_limiter() -> None:
    app = FastAPI()
    app.state.api_limiter = SharedQuotaLimiter(
        InMemoryRateLimitStore(), requests_limit=1, window_seconds=60.0
    )
    app.state.container = SimpleNamespace(
        config=SimpleNamespace(
            admin_api_token="adm",
            backend_api_token="view",
            trusted_proxy_client_ip=False,
        )
    )

    @app.get("/limited")
    async def limited(_: None = Depends(rate_limit_viewer)) -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        first = client.get("/limited", headers={"authorization": "Bearer view"})
        second = client.get("/limited", headers={"authorization": "Bearer view"})
        admin = client.get("/limited", headers={"authorization": "Bearer adm"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert admin.status_code == 200
