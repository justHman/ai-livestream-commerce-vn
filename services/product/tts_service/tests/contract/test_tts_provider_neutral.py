"""Provider-neutrality contract for POST /v1/speech (Change T task 1.2).

The backend-facing synthesis API must remain provider-neutral after the
runtime migration: provider-specific payloads (speaker embeddings, reference
codes, raw tensors) never serialize through the public schema, and the new
scheduling/tracing fields are accepted with safe defaults.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tts.api.v1.schemas.speech import SpeechRequest


def test_provider_embedding_fields_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        SpeechRequest.model_validate({"text": "xin chào", "speaker_emb": [0.1, 0.2]})


def test_provider_ref_codes_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        SpeechRequest.model_validate({"text": "xin chào", "ref_codes": {"code": [1, 2]}})


def test_provider_tensor_field_rejected_by_schema() -> None:
    with pytest.raises(ValidationError):
        SpeechRequest.model_validate({"text": "xin chào", "tensor": [[0.0, 1.0]]})


def test_scheduling_fields_accepted_with_defaults() -> None:
    req = SpeechRequest.model_validate({"text": "xin chào"})
    assert req.session_id is None
    assert req.utterance_id is None
    assert req.chunk_seq == 0
    assert req.voice_profile_id is None
    assert req.style == "natural"
    assert req.priority == "normal"


def test_scheduling_fields_accepted_when_provided() -> None:
    req = SpeechRequest.model_validate(
        {
            "text": "xin chào",
            "session_id": "sess-1",
            "utterance_id": "utt-1",
            "chunk_seq": 4,
            "voice_profile_id": "vp-1",
            "style": "vui_ve",
            "priority": "high",
        }
    )
    assert req.session_id == "sess-1"
    assert req.utterance_id == "utt-1"
    assert req.chunk_seq == 4
    assert req.voice_profile_id == "vp-1"
    assert req.style == "vui_ve"
    assert req.priority == "high"


def test_invalid_priority_rejected() -> None:
    with pytest.raises(ValidationError):
        SpeechRequest.model_validate({"text": "xin chào", "priority": "urgent"})


def test_negative_chunk_seq_rejected() -> None:
    with pytest.raises(ValidationError):
        SpeechRequest.model_validate({"text": "xin chào", "chunk_seq": -1})


def test_schema_has_no_provider_payload_field() -> None:
    schema = SpeechRequest.model_json_schema()
    properties = schema["properties"]
    for banned in ("speaker_emb", "ref_codes", "tensor", "audio"):
        assert banned not in properties, f"schema must not expose {banned}"


def test_openapi_speech_schema_has_no_provider_fields() -> None:
    from tts import create_app

    app = create_app()
    spec = app.openapi()
    schema = spec["components"]["schemas"]["SpeechRequest"]
    for banned in ("speaker_emb", "ref_codes"):
        assert banned not in schema["properties"], f"openapi must not expose {banned}"
    assert "additionalProperties" in schema
    assert schema["additionalProperties"] is False, "openapi must forbid extra fields"
