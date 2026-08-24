"""ObjectStore seam: provider-neutral storage for binary/reference voice assets."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest


class _Boto3ImportBlocker:
    """sys.meta_path finder that makes ``import boto3`` raise ImportError."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "boto3":
            raise ImportError("boto3 blocked")
        return None


class FakeS3:
    """Minimal S3 client with boto3's NoSuchKey shape."""

    class exceptions:
        class NoSuchKey(Exception):  # noqa: N801 — mirrors boto3 naming
            pass

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    def put_object(self, **kwargs) -> None:
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = bytes(kwargs["Body"])

    def get_object(self, **kwargs) -> dict:
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise self.exceptions.NoSuchKey(str(key))
        return {"Body": io.BytesIO(self.objects[key])}

    def delete_object(self, **kwargs) -> None:
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)


def test_object_store_module_exists_and_roundtrips_locally(tmp_path: Path) -> None:
    import tts.voices.object_store as object_store

    store = object_store.FilesystemObjectStore(tmp_path)
    uri = store.put("tenants/t1/ref.wav", b"RIFF...")
    assert uri.startswith("file://")
    assert store.get(uri) == b"RIFF..."
    store.delete(uri)
    with pytest.raises(ValueError):
        store.get(uri)


def test_s3_object_store_uses_injected_client_without_boto3() -> None:
    import tts.voices.object_store as object_store

    blocker = _Boto3ImportBlocker()
    sys.meta_path.insert(0, blocker)
    try:
        fake_client = FakeS3()
        store = object_store.S3ObjectStore("bkt", "voices", client=fake_client)
        uri = store.put("tenants/t1/ref.wav", b"RIFF...")
        assert uri == "s3://bkt/voices/tenants/t1/ref.wav"
        assert store.get(uri) == b"RIFF..."
        assert ("bkt", "voices/tenants/t1/ref.wav") in fake_client.objects
        store.delete(uri)
        with pytest.raises(ValueError):
            store.get(uri)
    finally:
        sys.meta_path.remove(blocker)
    assert "boto3" not in sys.modules


def test_get_object_store_factory_schemes(tmp_path: Path, monkeypatch) -> None:
    import tts.voices.object_store as object_store
    from tts.voices.object_store import get_object_store

    file_store = get_object_store("file://voice_refs", tmp_path)
    assert isinstance(file_store, object_store.FilesystemObjectStore)
    uri = file_store.put("tenants/t1/ref.wav", b"RIFF...")
    assert Path(uri.removeprefix("file://")) == tmp_path / "voice_refs" / "tenants/t1/ref.wav"
    assert file_store.get(uri) == b"RIFF..."

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3Module(FakeS3()))
    s3_store = get_object_store("s3://bkt/prefix", tmp_path)
    assert isinstance(s3_store, object_store.S3ObjectStore)
    s3_uri = s3_store.put("k.bin", b"data")
    assert s3_uri == "s3://bkt/prefix/k.bin"

    with pytest.raises(ValueError):
        get_object_store("gs://x", tmp_path)


class _FakeBoto3Module:
    """Stand-in for the real boto3 module so the factory never needs it."""

    def __init__(self, client: FakeS3) -> None:
        self._client = client

    def client(self, service_name: str) -> FakeS3:
        assert service_name == "s3"
        return self._client
