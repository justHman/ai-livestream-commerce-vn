"""VieNeu payload schema: JSON round-trip and schema versioning (4.5)."""

from __future__ import annotations

import json

import pytest

from tts.voices.payloads import (
    PAYLOAD_PROVIDER,
    PAYLOAD_SCHEMA_VERSION,
    InvalidPayloadError,
    decode_vieneu_payload,
    encode_vieneu_payload,
)


def test_encode_shape_has_schema_version() -> None:
    payload = encode_vieneu_payload(model_revision="rev-1", speaker_emb=[0.1, 0.2])
    assert payload["schema_version"] == PAYLOAD_SCHEMA_VERSION
    assert payload["provider"] == PAYLOAD_PROVIDER
    assert payload["ref_codes"] is None
    assert payload["style"] == "natural"
    assert payload["denoise"] is True


def test_json_round_trip_preserves_payload() -> None:
    payload = encode_vieneu_payload(
        model_revision="rev-1",
        speaker_emb=[0.1, 0.2, -0.3],
        ref_codes=[1.0, 2.0],
        style="doc_truyen",
        denoise=False,
    )
    decoded = decode_vieneu_payload(json.loads(json.dumps(payload)))
    assert decoded == payload


def test_unknown_schema_version_rejected() -> None:
    payload = encode_vieneu_payload(model_revision="rev-1", speaker_emb=[0.1])
    payload["schema_version"] = 99
    with pytest.raises(InvalidPayloadError, match="schema_version"):
        decode_vieneu_payload(payload)


def test_wrong_provider_rejected() -> None:
    payload = encode_vieneu_payload(model_revision="rev-1", speaker_emb=[0.1])
    payload["provider"] = "elevenlabs"
    with pytest.raises(InvalidPayloadError, match="provider"):
        decode_vieneu_payload(payload)


def test_missing_required_fields_rejected() -> None:
    payload = encode_vieneu_payload(model_revision="rev-1", speaker_emb=[0.1])
    del payload["speaker_emb"]
    with pytest.raises(InvalidPayloadError, match="missing required"):
        decode_vieneu_payload(payload)


def test_non_dict_payload_rejected() -> None:
    with pytest.raises(InvalidPayloadError, match="JSON object"):
        decode_vieneu_payload(["not", "a", "dict"])
