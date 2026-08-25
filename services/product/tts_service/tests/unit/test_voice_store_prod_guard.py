"""Production voice-store guard (R6.2 / BLOCKER 5).

Production (``APP_ENV`` in {prod, production}) must never silently fall back
to the local filesystem voice store (``file://``) — voice profiles would
vanish on task replacement/restart. Configuration must fail loud unless an
explicit test-only mode is selected; a durable provider-neutral URI (e.g.
``s3://``) is accepted.
"""

from __future__ import annotations

import pytest

from tts.config import load_runtime_config


def test_prod_default_voice_store_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.delenv("TTS_VOICE_STORE_URI", raising=False)
    with pytest.raises(ValueError, match="durable voice store"):
        load_runtime_config()


def test_prod_explicit_file_uri_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TTS_VOICE_STORE_URI", "file:///tmp/voice_profiles")
    with pytest.raises(ValueError, match="durable voice store"):
        load_runtime_config()


def test_prod_durable_uri_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TTS_VOICE_STORE_URI", "s3://voices-bucket/prefix")
    cfg = load_runtime_config()
    assert cfg.voice_store_uri == "s3://voices-bucket/prefix"


def test_prod_file_uri_accepted_when_test_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("TTS_VOICE_STORE_URI", "file:///tmp/voice_profiles")
    monkeypatch.setenv("TTS_VOICE_STORE_TEST_ONLY", "1")
    cfg = load_runtime_config()
    assert cfg.voice_store_uri == "file:///tmp/voice_profiles"
