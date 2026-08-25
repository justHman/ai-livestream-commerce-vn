"""Voice profile stores: interface, filesystem store, and factory (tasks 4.2-4.4).

The store owns metadata + provider payload together (one JSON document per
profile). Payloads are opaque dicts to the store — the schema versioning lives
in ``tts.voices.payloads``.

Seam split: ``VoiceProfileStore`` is the durable METADATA seam — one small
JSON document per profile that survives restart/replacement when pointed at a
durable URI such as ``s3://`` (production uses ``S3VoiceProfileStore`` so a
recreated TTS task reloads every profile by id from shared object storage).
Binary/reference assets (reference audio, larger artifacts) resolve through
``tts.voices.object_store.ObjectStore`` URIs instead. Core TTS code depends on
these protocols, never on boto3.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from tts.providers.errors import ProfileNotFoundError
from tts.voices.models import VoiceProfile

DEFAULT_TENANT_ID = "default"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class VoiceProfileStore(Protocol):
    """Durable METADATA seam: one small JSON doc per profile (metadata+payload).

    Survives restart/replacement when configured with a durable URI such as
    ``s3://``. Binary/reference assets (reference audio, larger artifacts)
    resolve through ``tts.voices.object_store.ObjectStore`` URIs, not here.
    """

    def save_profile(self, profile: VoiceProfile, payload: dict) -> None: ...

    def load_profile(self, voice_profile_id: str, tenant_id: str) -> tuple[VoiceProfile, dict]:
        """Return (metadata, payload); raise ProfileNotFoundError when missing."""
        ...

    def delete_profile(self, voice_profile_id: str, tenant_id: str) -> None: ...

    def list_profiles(self, tenant_id: str) -> list[VoiceProfile]: ...


class FilesystemVoiceProfileStore:
    """File-backed store: one ``<root>/<tenant>/<id>.json`` per profile.

    Writes are atomic (temp file + ``os.replace``). The doc stores
    ``{"metadata": {...}, "payload": {...}}`` so a restart rehydrates both.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _profile_path(self, tenant_id: str, voice_profile_id: str) -> Path:
        return self._root / tenant_id / f"{voice_profile_id}.json"

    def save_profile(self, profile: VoiceProfile, payload: dict) -> None:
        target = self._profile_path(profile.tenant_id, profile.voice_profile_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        doc = {"metadata": _metadata_to_json(profile), "payload": payload}
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(doc, handle, ensure_ascii=False)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def load_profile(self, voice_profile_id: str, tenant_id: str) -> tuple[VoiceProfile, dict]:
        path = self._profile_path(tenant_id, voice_profile_id)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found for tenant {tenant_id!r}"
            ) from exc
        metadata = doc["metadata"]
        if metadata.get("voice_profile_id") != voice_profile_id:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} corrupted: stored id "
                f"{metadata.get('voice_profile_id')!r} does not match"
            )
        if metadata.get("tenant_id") != tenant_id:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found for tenant {tenant_id!r}"
            )
        return _profile_from_json(metadata), doc["payload"]

    def delete_profile(self, voice_profile_id: str, tenant_id: str) -> None:
        path = self._profile_path(tenant_id, voice_profile_id)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found for tenant {tenant_id!r}"
            ) from exc

    def list_profiles(self, tenant_id: str) -> list[VoiceProfile]:
        tenant_dir = self._root / tenant_id
        if not tenant_dir.is_dir():
            return []
        profiles = []
        for path in sorted(tenant_dir.glob("*.json")):
            try:
                profile, _ = self.load_profile(path.stem, tenant_id)
            except ProfileNotFoundError:
                continue
            profiles.append(profile)
        return profiles


def _metadata_to_json(profile: VoiceProfile) -> dict:
    metadata = asdict(profile)
    metadata["created_at"] = profile.created_at.isoformat()
    return metadata


def _profile_from_json(metadata: dict) -> VoiceProfile:
    from datetime import datetime

    created_at = metadata.get("created_at")
    return VoiceProfile(
        voice_profile_id=metadata["voice_profile_id"],
        tenant_id=metadata["tenant_id"],
        provider_name=metadata["provider_name"],
        provider_model_revision=metadata["provider_model_revision"],
        profile_kind=metadata["profile_kind"],
        display_name=metadata["display_name"],
        provider_payload_location=metadata["provider_payload_location"],
        created_at=datetime.fromisoformat(created_at) if created_at else _now_utc(),
    )


class S3VoiceProfileStore:
    """Restart-safe shared store over S3 (task 4.4).

    Key layout mirrors the filesystem store: ``<tenant_id>/<voice_profile_id>.json``.
    boto3 is imported lazily so tests and CPU-only installs never require it.
    """

    def __init__(self, bucket: str, prefix: str = "", *, client: object | None = None) -> None:
        if client is None:
            import boto3  # lazy: only when an s3:// store URI is configured

            client = boto3.client("s3")
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._client = client

    def _key(self, tenant_id: str, voice_profile_id: str) -> str:
        base = f"{tenant_id}/{voice_profile_id}.json"
        return f"{self._prefix}/{base}" if self._prefix else base

    def save_profile(self, profile: VoiceProfile, payload: dict) -> None:
        doc = json.dumps(
            {"metadata": _metadata_to_json(profile), "payload": payload}, ensure_ascii=False
        )
        self._client.put_object(
            Bucket=self._bucket,
            Key=self._key(profile.tenant_id, profile.voice_profile_id),
            Body=doc.encode("utf-8"),
            ContentType="application/json",
        )

    def load_profile(self, voice_profile_id: str, tenant_id: str) -> tuple[VoiceProfile, dict]:
        try:
            response = self._client.get_object(
                Bucket=self._bucket, Key=self._key(tenant_id, voice_profile_id)
            )
        except self._client.exceptions.NoSuchKey as exc:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found for tenant {tenant_id!r}"
            ) from exc
        doc = json.loads(response["Body"].read().decode("utf-8"))
        metadata = doc["metadata"]
        if metadata.get("voice_profile_id") != voice_profile_id:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} corrupted: stored id "
                f"{metadata.get('voice_profile_id')!r} does not match"
            )
        if metadata.get("tenant_id") != tenant_id:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found for tenant {tenant_id!r}"
            )
        return _profile_from_json(metadata), doc["payload"]

    def delete_profile(self, voice_profile_id: str, tenant_id: str) -> None:
        try:
            self._client.delete_object(
                Bucket=self._bucket, Key=self._key(tenant_id, voice_profile_id)
            )
        except self._client.exceptions.NoSuchKey as exc:
            raise ProfileNotFoundError(
                f"voice profile {voice_profile_id!r} not found for tenant {tenant_id!r}"
            ) from exc

    def list_profiles(self, tenant_id: str) -> list[VoiceProfile]:
        prefix = f"{self._prefix}/{tenant_id}/" if self._prefix else f"{tenant_id}/"
        paginator = self._client.get_paginator("list_objects_v2")
        profiles = []
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                profile_id = key.rsplit("/", 1)[-1][: -len(".json")]
                try:
                    profile, _ = self.load_profile(profile_id, tenant_id)
                except ProfileNotFoundError:
                    continue
                profiles.append(profile)
        return profiles


def get_store(uri: str, runtime_root: Path) -> VoiceProfileStore:
    """Build a store from a ``file://`` or ``s3://`` URI.

    Unknown schemes fail fast at startup with a clear configuration error.
    ``file://`` resolves relative to ``runtime_root`` (the default URI points
    at ``runtime_root/voice_profiles``).
    """
    scheme, _, rest = uri.partition("://")
    if scheme == "file":
        path = Path(rest)
        if not path.is_absolute():
            path = Path(runtime_root) / path
        return FilesystemVoiceProfileStore(path)
    if scheme == "s3":
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise ValueError(f"invalid voice store URI {uri!r}: s3 bucket missing")
        return S3VoiceProfileStore(bucket, prefix)
    raise ValueError(
        f"unsupported voice store URI {uri!r}: expected file://... or s3://bucket[/prefix]"
    )
