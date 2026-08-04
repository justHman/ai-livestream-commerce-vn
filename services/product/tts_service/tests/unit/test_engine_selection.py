"""Engine selection and vocabulary for the TTS service."""

from __future__ import annotations

import pytest

from tts.engines.base import ENGINES, load_engine
from tts.engines.base import ToneEngine


def test_self_host_engines_registered() -> None:
    assert {"vieneu", "cosyvoice"} <= set(ENGINES)


def test_transformers_registered_for_legacy_fallback() -> None:
    # d6d2880 restored transformers (registered as 'transformers' + 'hf') for
    # the legacy offline fallback path; parity requires it to be loadable.
    assert "transformers" in ENGINES


def test_remote_http_not_registered() -> None:
    assert "remote_http" not in ENGINES


def test_elevenlabs_not_registered() -> None:
    assert "elevenlabs" not in ENGINES


def test_load_tone_when_none() -> None:
    e = load_engine({"engine": "none"})
    assert isinstance(e, ToneEngine)


def test_load_unknown_rejected() -> None:
    with pytest.raises(KeyError):
        load_engine({"engine": "remote_http"})
