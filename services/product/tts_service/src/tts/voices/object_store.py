"""Provider-neutral ObjectStore seam for binary/reference voice assets.

``VoiceProfileStore`` (``tts.voices.store``) is the durable METADATA seam: one
small JSON document per profile. Binary/reference assets — reference audio,
larger artifacts — resolve through this module's ``ObjectStore`` URIs instead.
Core TTS code depends on these protocols, never on boto3: local filesystem
backing is an explicit dev/test mode only, and production deployments configure
an ``s3://`` store (boto3 is imported lazily inside the S3 adapter).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol


class ObjectStore(Protocol):
    """Storage seam for opaque binary objects addressed by key and URI."""

    def put(self, key: str, data: bytes) -> str:
        """Store ``data`` under ``key``; return the object's opaque URI."""
        ...

    def get(self, uri: str) -> bytes:
        """Return the bytes stored at ``uri``; raise ValueError when unknown."""
        ...

    def delete(self, uri: str) -> None:
        """Remove the object at ``uri``; raise ValueError when unknown."""
        ...


class FilesystemObjectStore:
    """Local filesystem backing for ``ObjectStore`` — dev/test mode only.

    Production deployments configure ``s3://`` instead. Writes are atomic
    (temp file + ``os.replace``). URIs are ``file://<absolute-path>`` and are
    only accepted back when they resolve inside this store's root.
    """

    def __init__(self, root: Path) -> None:
        self._root = Path(root).resolve()

    def _path_for_uri(self, uri: str) -> Path:
        scheme, _, rest = uri.partition("://")
        if scheme != "file":
            raise ValueError(f"unknown object uri {uri!r}: expected file://...")
        resolved = Path(rest).resolve()
        try:
            resolved.relative_to(self._root)
        except ValueError as exc:
            raise ValueError(f"unknown object uri {uri!r}: outside store root") from exc
        return resolved

    def put(self, key: str, data: bytes) -> str:
        target = self._root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, target)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return f"file://{target.resolve().as_posix()}"

    def get(self, uri: str) -> bytes:
        path = self._path_for_uri(uri)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ValueError(f"unknown object uri {uri!r}") from exc

    def delete(self, uri: str) -> None:
        path = self._path_for_uri(uri)
        try:
            path.unlink()
        except FileNotFoundError as exc:
            raise ValueError(f"unknown object uri {uri!r}") from exc


class S3ObjectStore:
    """S3-backed ``ObjectStore``; boto3 is imported lazily in ``__init__``."""

    def __init__(self, bucket: str, prefix: str = "", *, client: object | None = None) -> None:
        if client is None:
            import boto3  # lazy: only when an s3:// object store URI is configured

            client = boto3.client("s3")
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._client = client

    def _key(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def _parse_own_uri(self, uri: str) -> tuple[str, str]:
        scheme, _, rest = uri.partition("://")
        if scheme != "s3":
            raise ValueError(f"unknown object uri {uri!r}: expected s3://bucket/key")
        bucket, _, obj_key = rest.partition("/")
        if not bucket or not obj_key or bucket != self._bucket:
            raise ValueError(f"unknown object uri {uri!r}")
        return bucket, obj_key

    def put(self, key: str, data: bytes) -> str:
        full_key = self._key(key)
        self._client.put_object(Bucket=self._bucket, Key=full_key, Body=data)
        return f"s3://{self._bucket}/{full_key}"

    def get(self, uri: str) -> bytes:
        bucket, obj_key = self._parse_own_uri(uri)
        try:
            response = self._client.get_object(Bucket=bucket, Key=obj_key)
        except self._client.exceptions.NoSuchKey as exc:
            raise ValueError(f"unknown object uri {uri!r}") from exc
        return response["Body"].read()

    def delete(self, uri: str) -> None:
        bucket, obj_key = self._parse_own_uri(uri)
        self._client.delete_object(Bucket=bucket, Key=obj_key)


def get_object_store(uri: str, runtime_root: Path) -> ObjectStore:
    """Build an ``ObjectStore`` from a ``file://`` or ``s3://`` URI.

    Unknown schemes fail fast with a clear configuration error. ``file://``
    resolves relative paths against ``runtime_root``; local filesystem backing
    is dev/test only — production configures ``s3://bucket[/prefix]``.
    """
    scheme, _, rest = uri.partition("://")
    if scheme == "file":
        path = Path(rest)
        if not path.is_absolute():
            path = Path(runtime_root) / path
        return FilesystemObjectStore(path)
    if scheme == "s3":
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise ValueError(f"invalid object store URI {uri!r}: s3 bucket missing")
        return S3ObjectStore(bucket, prefix)
    raise ValueError(
        f"unsupported object store URI {uri!r}: expected file://... or s3://bucket[/prefix]"
    )
