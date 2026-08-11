"""Preset and cloned voices share one `voice_profile_id` field (task 2.6).

Callers select either a preset or an enrolled cloned voice through the same
opaque external ID; the provider layer resolves it. Nothing in the public
schema distinguishes preset from clone.
"""

from __future__ import annotations

from tts.api.v1.schemas.speech import SpeechRequest
from tts.providers.models import SynthesisRequest


def test_preset_voice_uses_voice_profile_id() -> None:
    req = SpeechRequest.model_validate(
        {"text": "xin chào", "voice_profile_id": "vp-preset-tu-nhien"}
    )
    assert req.voice_profile_id == "vp-preset-tu-nhien"


def test_cloned_voice_uses_voice_profile_id() -> None:
    req = SpeechRequest.model_validate({"text": "xin chào", "voice_profile_id": "vp-clone-01"})
    assert req.voice_profile_id == "vp-clone-01"


def test_domain_request_carries_voice_profile_id_for_both_kinds() -> None:
    for profile_id in ("vp-preset-tu-nhien", "vp-clone-01"):
        req = SynthesisRequest(
            request_id="r1",
            session_id="s1",
            utterance_id="u1",
            chunk_seq=0,
            input_text="xin chào",
            voice_profile_id=profile_id,
        )
        assert req.voice_profile_id == profile_id


def test_voice_profile_id_defaults_to_default() -> None:
    req = SpeechRequest.model_validate({"text": "xin chào"})
    assert req.voice_profile_id is None  # engine-level default voice still applies
