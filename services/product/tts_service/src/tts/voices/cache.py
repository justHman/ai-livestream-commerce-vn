"""Bounded LRU cache for decoded voice profiles (Change T tasks 4.6/4.7).

The store remains the source of truth; the cache only avoids re-reading a
payload on hot synthesis paths. Bounded by ``maxsize``; keys are
``(tenant_id, voice_profile_id)`` so tenants never share cache entries.
The FastAPI single event loop owns these caches (scheduler access stays on
one loop), so a plain ``OrderedDict`` is enough — no lock.

Metrics (task 12.4): hit/miss/eviction counters via an optional
``MetricsRegistry``; profile ids never become metric labels.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Optional

from tts.observability.metrics import MetricsRegistry
from tts.voices.models import VoiceProfile


class VoiceProfileCache:
    """LRU cache keyed by ``(tenant_id, voice_profile_id)``."""

    def __init__(self, maxsize: int = 256, metrics: Optional[MetricsRegistry] = None) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._metrics = metrics
        self._entries: OrderedDict[tuple[str, str], tuple[VoiceProfile, dict]] = OrderedDict()

    def _key(self, tenant_id: str, voice_profile_id: str) -> tuple[str, str]:
        return (tenant_id, voice_profile_id)

    def get(self, tenant_id: str, voice_profile_id: str) -> tuple[VoiceProfile, dict] | None:
        key = self._key(tenant_id, voice_profile_id)
        entry = self._entries.get(key)
        if entry is None:
            if self._metrics is not None:
                self._metrics.incr("voice_cache_miss_total")
            return None
        if self._metrics is not None:
            self._metrics.incr("voice_cache_hit_total")
        self._entries.move_to_end(key)
        return entry

    def put(
        self, tenant_id: str, voice_profile_id: str, profile: VoiceProfile, payload: dict
    ) -> None:
        key = self._key(tenant_id, voice_profile_id)
        self._entries[key] = (profile, payload)
        self._entries.move_to_end(key)
        while len(self._entries) > self._maxsize:
            self._entries.popitem(last=False)
            if self._metrics is not None:
                self._metrics.incr("voice_cache_eviction_total")

    def evict(self, tenant_id: str, voice_profile_id: str) -> None:
        self._entries.pop(self._key(tenant_id, voice_profile_id), None)

    def invalidate_tenant(self, tenant_id: str) -> None:
        stale = [key for key in self._entries if key[0] == tenant_id]
        for key in stale:
            self._entries.pop(key, None)

    def __len__(self) -> int:
        return len(self._entries)


class CachedVoiceProfileStore:
    """Store facade that reads through the LRU cache (task 4.6).

    Writes/evictions update the cache inline so a delete is immediately
    visible; the persistent store stays authoritative across restarts.
    """

    def __init__(
        self, store: object, maxsize: int = 256, metrics: Optional[MetricsRegistry] = None
    ) -> None:
        self._store = store
        self._cache = VoiceProfileCache(maxsize=maxsize, metrics=metrics)

    def save_profile(self, profile: VoiceProfile, payload: dict) -> None:
        self._store.save_profile(profile, payload)
        self._cache.put(profile.tenant_id, profile.voice_profile_id, profile, payload)

    def load_profile(self, voice_profile_id: str, tenant_id: str) -> tuple[VoiceProfile, dict]:
        cached = self._cache.get(tenant_id, voice_profile_id)
        if cached is not None:
            return cached
        profile, payload = self._store.load_profile(voice_profile_id, tenant_id)
        self._cache.put(tenant_id, voice_profile_id, profile, payload)
        return profile, payload

    def delete_profile(self, voice_profile_id: str, tenant_id: str) -> None:
        self._cache.evict(tenant_id, voice_profile_id)
        self._store.delete_profile(voice_profile_id, tenant_id)

    def list_profiles(self, tenant_id: str) -> list[VoiceProfile]:
        return self._store.list_profiles(tenant_id)
