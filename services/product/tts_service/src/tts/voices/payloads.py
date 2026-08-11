"""Serialized provider payload schema (Change T task 4.5).

The provider adapter (cluster 4) stores exactly this shape per enrolled
profile; ``encode_vieneu_payload``/``decode_vieneu_payload`` keep the schema
version check in one place so an old profile never silently misloads.
"""

from __future__ import annotations

from typing import Any

PAYLOAD_SCHEMA_VERSION = 1
PAYLOAD_PROVIDER = "vieneu_v3"


class InvalidPayloadError(ValueError):
    """Stored payload failed schema validation."""


def encode_vieneu_payload(
    *,
    model_revision: str,
    speaker_emb: list[float],
    ref_codes: list[float] | None = None,
    style: str = "natural",
    denoise: bool = True,
) -> dict[str, Any]:
    """Encode a provider payload for persistence."""
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "provider": PAYLOAD_PROVIDER,
        "model_revision": model_revision,
        "speaker_emb": speaker_emb,
        "ref_codes": ref_codes,
        "style": style,
        "denoise": denoise,
    }


def decode_vieneu_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a stored payload; raise ``InvalidPayloadError``."""
    if not isinstance(payload, dict):
        raise InvalidPayloadError("payload must be a JSON object")
    if payload.get("schema_version") != PAYLOAD_SCHEMA_VERSION:
        raise InvalidPayloadError(
            f"unsupported payload schema_version {payload.get('schema_version')!r}; "
            f"expected {PAYLOAD_SCHEMA_VERSION}"
        )
    if payload.get("provider") != PAYLOAD_PROVIDER:
        raise InvalidPayloadError(
            f"payload provider {payload.get('provider')!r} is not {PAYLOAD_PROVIDER!r}"
        )
    required = ("model_revision", "speaker_emb")
    missing = [name for name in required if not payload.get(name)]
    if missing:
        raise InvalidPayloadError(f"payload missing required fields: {missing}")
    return payload
