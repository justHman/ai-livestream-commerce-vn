"""Preset voice seeding: idempotent, kind preset, placeholder location (5.5)."""

from __future__ import annotations

import pytest

from tts.voices.presets import PRESET_VOICE_NAMES, seed_preset_profiles
from tts.voices.store import FilesystemVoiceProfileStore


@pytest.fixture
def store(tmp_path) -> FilesystemVoiceProfileStore:
    return FilesystemVoiceProfileStore(tmp_path / "vp")


def test_seed_creates_all_presets(store: FilesystemVoiceProfileStore) -> None:
    profiles = seed_preset_profiles(
        store, "tenant-a", provider_name="vieneu_v3", model_revision="rev-1"
    )
    assert len(profiles) == len(PRESET_VOICE_NAMES)
    assert {p.profile_kind for p in profiles} == {"preset"}
    assert {p.display_name for p in profiles} == set(PRESET_VOICE_NAMES)


def test_seed_is_idempotent(store: FilesystemVoiceProfileStore) -> None:
    seed_preset_profiles(store, "t", provider_name="vieneu_v3", model_revision="rev-1")
    seed_preset_profiles(store, "t", provider_name="vieneu_v3", model_revision="rev-1")
    assert len(store.list_profiles("t")) == len(PRESET_VOICE_NAMES)  # no duplicates


def test_seed_profiles_carry_preset_payload_location(store: FilesystemVoiceProfileStore) -> None:
    profiles = seed_preset_profiles(store, "t", provider_name="vieneu_v3", model_revision="rev-1")
    assert {p.provider_payload_location for p in profiles} == {
        f"preset://{name}" for name in PRESET_VOICE_NAMES
    }


def test_seed_is_tenant_scoped(store: FilesystemVoiceProfileStore) -> None:
    seed_preset_profiles(store, "tenant-a", provider_name="vieneu_v3", model_revision="rev-1")
    assert store.list_profiles("tenant-b") == []
