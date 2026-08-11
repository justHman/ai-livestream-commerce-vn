"""VoiceProfileCache: hit/miss/eviction and cache-through store (4.6/4.7)."""

from __future__ import annotations

import pytest

from tts.providers.errors import ProfileNotFoundError
from tts.voices.cache import CachedVoiceProfileStore, VoiceProfileCache
from tts.voices.store import FilesystemVoiceProfileStore

from tests.unit.test_voices_helpers import make_profile


@pytest.fixture
def cache() -> VoiceProfileCache:
    return VoiceProfileCache(maxsize=2)


def test_get_miss_returns_none(cache: VoiceProfileCache) -> None:
    assert cache.get("tenant-a", "vp-x") is None


def test_get_hit_after_put(cache: VoiceProfileCache) -> None:
    profile = make_profile("vp-1", "tenant-a")
    cache.put("tenant-a", "vp-1", profile, {"k": 1})
    cached = cache.get("tenant-a", "vp-1")
    assert cached == (profile, {"k": 1})


def test_lru_eviction_drops_oldest(cache: VoiceProfileCache) -> None:
    cache.put("t", "vp-1", make_profile("vp-1", "t"), {})
    cache.put("t", "vp-2", make_profile("vp-2", "t"), {})
    cache.get("t", "vp-1")  # refresh vp-1 so vp-2 becomes oldest
    cache.put("t", "vp-3", make_profile("vp-3", "t"), {})
    assert cache.get("t", "vp-2") is None  # evicted
    assert cache.get("t", "vp-1") is not None
    assert cache.get("t", "vp-3") is not None


def test_evict_removes_entry(cache: VoiceProfileCache) -> None:
    cache.put("t", "vp-1", make_profile("vp-1", "t"), {})
    cache.evict("t", "vp-1")
    assert cache.get("t", "vp-1") is None


def test_invalidate_tenant_keeps_others(cache: VoiceProfileCache) -> None:
    cache.put("a", "vp-1", make_profile("vp-1", "a"), {})
    cache.put("b", "vp-2", make_profile("vp-2", "b"), {})
    cache.invalidate_tenant("a")
    assert cache.get("a", "vp-1") is None
    assert cache.get("b", "vp-2") is not None


def test_cache_through_store_loads_once(tmp_path) -> None:
    """A second load for the same key is served from cache (no store call)."""
    store = FilesystemVoiceProfileStore(tmp_path / "vp")
    cached = CachedVoiceProfileStore(store, maxsize=4)
    profile = make_profile("vp-1", "t")
    store.save_profile(profile, {"k": 1})

    first, payload = cached.load_profile("vp-1", "t")
    second, _ = cached.load_profile("vp-1", "t")
    assert first == second == profile
    assert payload == {"k": 1}
    # Deleting the file behind the cache does not change a cached read.
    (tmp_path / "vp" / "t" / "vp-1.json").unlink()
    third, _ = cached.load_profile("vp-1", "t")
    assert third == profile


def test_delete_through_cache_evicts(tmp_path) -> None:
    store = FilesystemVoiceProfileStore(tmp_path / "vp")
    cached = CachedVoiceProfileStore(store, maxsize=4)
    store.save_profile(make_profile("vp-1", "t"), {})
    cached.load_profile("vp-1", "t")  # populate cache
    cached.delete_profile("vp-1", "t")
    with pytest.raises(ProfileNotFoundError):
        cached.load_profile("vp-1", "t")


def test_restart_reload_pops_cache_after_new_store(tmp_path) -> None:
    """A fresh store + fresh cache over the same dir reloads from disk (4.7)."""
    root = tmp_path / "vp"
    first_store = FilesystemVoiceProfileStore(root)
    first_store.save_profile(make_profile("vp-r", "t", kind="cloned"), {"v": 2})

    second_store = FilesystemVoiceProfileStore(root)
    second_cached = CachedVoiceProfileStore(second_store, maxsize=4)
    profile, payload = second_cached.load_profile("vp-r", "t")
    assert profile.voice_profile_id == "vp-r"
    assert payload == {"v": 2}


def test_maxsize_must_be_positive() -> None:
    with pytest.raises(ValueError, match="maxsize"):
        VoiceProfileCache(maxsize=0)
