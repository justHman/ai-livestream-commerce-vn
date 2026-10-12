import asyncio
import json

import fakeredis.aioredis
import pytest

from backend.application.clients.avatar.lemonslice_budget import DailyAvatarBudget


@pytest.fixture(autouse=True)
def deterministic_redis_utc_time(monkeypatch):
    # Redis TIME in the double must not land at midnight while a test builds its history.
    monkeypatch.setattr("time.time", lambda: 864000 + 3600.0)


def ledger(redis, limit=60):
    return DailyAvatarBudget("redis://unused", "fake-account", limit, redis=redis)


@pytest.mark.asyncio
async def test_spent_and_active_minutes_block_new_starts_across_restarts():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    budget = ledger(redis)
    now = (await redis.time())[0]
    await redis.set(
        budget.key,
        json.dumps({"paid": [now - 100, now - 40, now], "active": [now - 10, None, now + 60]}),
    )
    replacement = ledger(redis)
    assert await replacement.exceeded()
    with pytest.raises(RuntimeError, match="daily_avatar_limit"):
        await replacement.begin()
    assert "fake-account" not in budget.key
    await redis.aclose()


@pytest.mark.asyncio
async def test_concurrent_starts_are_accounted_and_confirmed_end_is_idempotent():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    first, second = ledger(redis), ledger(redis)
    a, b = await asyncio.gather(first.begin(), second.begin())
    assert a != b
    await first.finish(a)
    await first.finish(a)
    entries = json.loads(await redis.get(first.key))
    assert len(entries) == 2 and entries[a][1] is not None and entries[b][1] is None
    await redis.aclose()


def test_utc_midnight_clips_each_avatar_interval_without_losing_active_usage():
    day = 86400.0
    entries = {
        "ended": [day - 70, day - 10, day],
        "cross": [day - 40, day + 20, day + 30],
        "active": [day - 30, None, day + 200],
    }
    assert DailyAvatarBudget.usage(entries, day + 60) == 80
    assert DailyAvatarBudget.usage(entries, day + 120) == 140


@pytest.mark.asyncio
async def test_crashed_provider_is_charged_until_idle_expiry_and_not_forever_next_day():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    now = (await redis.time())[0]
    day = now // 86400 * 86400
    budget = ledger(redis)
    orphan = {"lost": [day - 1800, None, day - 120]}
    await redis.set(budget.key, json.dumps(orphan))
    assert DailyAvatarBudget.usage(orphan, day - 60) == 1680
    assert not await ledger(redis).exceeded()
    token = await ledger(redis).begin()
    assert list(json.loads(await redis.get(budget.key))) == [token]
    await redis.aclose()


@pytest.mark.asyncio
async def test_touch_retains_the_longest_in_flight_billing_lease():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    budget = ledger(redis)
    token = await budget.begin(130)
    before = json.loads(await redis.get(budget.key))[token][2]
    await budget.touch(token, 10)
    assert json.loads(await redis.get(budget.key))[token][2] == before
    await budget.touch(token, 200)
    assert json.loads(await redis.get(budget.key))[token][2] > before
    await redis.aclose()


@pytest.mark.asyncio
async def test_unavailable_shared_ledger_fails_closed():
    # A disconnected real endpoint is a failed admission, never an in-memory fallback.
    failing = DailyAvatarBudget("redis://127.0.0.1:1", "fake-account", 60)
    with pytest.raises(Exception):
        await failing.begin()
    await failing.close()
