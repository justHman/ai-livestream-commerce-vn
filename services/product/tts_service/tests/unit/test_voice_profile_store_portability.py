"""VoiceProfileStore portability: durable metadata seam, boto3-free local paths."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tts.config import load_runtime_config
from tts.voices.store import FilesystemVoiceProfileStore, get_store

from tests.unit.test_voices_helpers import make_profile


class _Boto3ImportBlocker:
    """sys.meta_path finder that makes ``import boto3`` raise ImportError."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "boto3":
            raise ImportError("boto3 blocked")
        return None


def test_metadata_survives_replacement_through_durable_seam(tmp_path: Path) -> None:
    """Instance A (process 1) saves; instance B (replacement process 2) reloads."""
    process_one = FilesystemVoiceProfileStore(tmp_path)
    profile = make_profile("vp-durable", "tenant-a", kind="cloned", display_name="Giọng store")
    payload = {"schema_version": 1, "speaker_embedding": [0.1, 0.2]}
    process_one.save_profile(profile, payload)

    process_two = FilesystemVoiceProfileStore(tmp_path)
    loaded, loaded_payload = process_two.load_profile("vp-durable", "tenant-a")
    assert loaded == profile
    assert loaded_payload == payload


def test_non_s3_path_runs_without_boto3(tmp_path: Path) -> None:
    from tts.voices.object_store import FilesystemObjectStore

    blocker = _Boto3ImportBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        profile_store = FilesystemVoiceProfileStore(tmp_path / "voice_profiles")
        profile = make_profile("vp-noboto3", "tenant-a")
        profile_store.save_profile(profile, {"k": "v"})
        loaded, _ = profile_store.load_profile("vp-noboto3", "tenant-a")
        assert loaded == profile
        assert [p.voice_profile_id for p in profile_store.list_profiles("tenant-a")] == [
            "vp-noboto3"
        ]
        profile_store.delete_profile("vp-noboto3", "tenant-a")

        object_store = FilesystemObjectStore(tmp_path / "voice_refs")
        uri = object_store.put("tenants/t1/ref.wav", b"RIFF...")
        assert object_store.get(uri) == b"RIFF..."
    finally:
        sys.meta_path.remove(blocker)
    assert "boto3" not in sys.modules


def test_default_voice_store_uri_is_local_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TTS_VOICE_STORE_URI", raising=False)
    runtime = load_runtime_config()
    assert runtime.voice_store_uri.startswith("file://")
    store = get_store(runtime.voice_store_uri, Path(".runtime").resolve())
    assert store.__class__ is FilesystemVoiceProfileStore
