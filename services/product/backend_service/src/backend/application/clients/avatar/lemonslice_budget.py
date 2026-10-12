"""Shared, restart-safe UTC avatar-minute admission ledger.

Pending/unknown outcomes are charged through the last possible provider idle expiry.
No key TTL: losing an active charge would reopen the spending gate after restart.
All mutations use WATCH/MULTI, so replicas sharing an API key share admission.
"""

from __future__ import annotations

import hashlib
import json
import uuid

from redis.asyncio import Redis
from redis.exceptions import WatchError


class DailyAvatarBudget:
    def __init__(self, url: str, api_key: str, limit_s: float, *, redis=None):
        self.redis = redis or Redis.from_url(
            url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2
        )
        self.key = "lemonslice:avatar-minutes:" + hashlib.sha256(api_key.encode()).hexdigest()
        self.limit_s = limit_s

    @staticmethod
    def usage(entries: dict, now: float) -> float:
        day = now // 86400 * 86400
        return sum(
            max(0.0, min(now, item[1] if item[1] is not None else item[2]) - max(day, item[0]))
            for item in entries.values()
        )

    async def _update(self, operation: str, token: str = "", survival_s: float = 0):
        for _ in range(8):
            async with self.redis.pipeline() as pipe:
                try:
                    await pipe.watch(self.key)
                    seconds, micros = await pipe.time()
                    now = seconds + micros / 1_000_000
                    entries = json.loads(await pipe.get(self.key) or "{}")
                    used = self.usage(entries, now)
                    if operation == "check":
                        return used >= self.limit_s
                    if operation == "begin":
                        if used >= self.limit_s:
                            raise RuntimeError("daily_avatar_limit")
                        # Bound unknown outcomes/ledger size: no eviction of active charges.
                        entries = {
                            k: v
                            for k, v in entries.items()
                            if (v[1] if v[1] is not None else v[2]) >= now // 86400 * 86400
                        }
                        if len(entries) >= 10000:
                            raise RuntimeError("daily_avatar_ledger_capacity")
                        entries[token] = [now, None, now + survival_s]
                    elif operation == "touch":
                        if token not in entries or entries[token][1] is not None:
                            raise RuntimeError("daily_avatar_charge_missing")
                        entries[token][2] = max(entries[token][2], now + survival_s)
                    elif token in entries and entries[token][1] is None:
                        entries[token][1] = min(now, entries[token][2])
                    pipe.multi()
                    pipe.set(self.key, json.dumps(entries, separators=(",", ":")))
                    await pipe.execute()
                    return token
                except WatchError:
                    continue
        raise RuntimeError("daily_avatar_ledger_busy")

    async def begin(self, survival_s: float = 130) -> str:
        return await self._update("begin", uuid.uuid4().hex, survival_s)

    async def touch(self, token: str, survival_s: float) -> None:
        await self._update("touch", token, survival_s)

    async def finish(self, token: str) -> None:
        await self._update("finish", token)

    async def exceeded(self) -> bool:
        return await self._update("check")

    async def close(self) -> None:
        await self.redis.aclose()
