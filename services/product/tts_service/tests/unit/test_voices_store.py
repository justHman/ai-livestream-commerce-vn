"""VoiceProfile store: filesystem persistence, tenant isolation, delete (4.3/4.8/4.9)."""

from __future__ import annotations

import io
import json

import pytest

from tts.providers.errors import ProfileNotFoundError
from tts.voices.models import new_voice_profile_id
from tts.voices.store import FilesystemVoiceProfileStore, S3VoiceProfileStore, get_store

from tests.unit.test_voices_helpers import make_profile


class FakeS3Client:
    """Minimal in-memory S3 client with boto3's NoSuchKey shape."""

    class exceptions:
        class NoSuchKey(Exception):  # noqa: N801 — mirrors boto3 naming
            pass

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs) -> None:
        self.objects[kwargs["Key"]] = kwargs["Body"]

    def get_object(self, **kwargs) -> dict:
        key = kwargs["Key"]
        if key not in self.objects:
            raise self.exceptions.NoSuchKey(key)
        return {"Body": io.BytesIO(self.objects[key])}

    def delete_object(self, **kwargs) -> None:
        self.objects.pop(kwargs["Key"], None)


@pytest.fixture
def store(tmp_path) -> FilesystemVoiceProfileStore:
    return FilesystemVoiceProfileStore(tmp_path / "voice_profiles")


def test_save_then_load_round_trips(store: FilesystemVoiceProfileStore) -> None:
    profile = make_profile("vp-1", "tenant-a", kind="cloned")
    store.save_profile(profile, {"schema_version": 1})
    loaded, payload = store.load_profile("vp-1", "tenant-a")
    assert loaded == profile
    assert payload == {"schema_version": 1}


def test_load_missing_profile_raises_not_found(store: FilesystemVoiceProfileStore) -> None:
    with pytest.raises(ProfileNotFoundError):
        store.load_profile("vp-nope", "tenant-a")


def test_delete_removes_profile(store: FilesystemVoiceProfileStore) -> None:
    profile = make_profile("vp-1", "tenant-a")
    store.save_profile(profile, {})
    store.delete_profile("vp-1", "tenant-a")
    with pytest.raises(ProfileNotFoundError):
        store.load_profile("vp-1", "tenant-a")


def test_delete_missing_profile_raises_not_found(store: FilesystemVoiceProfileStore) -> None:
    with pytest.raises(ProfileNotFoundError):
        store.delete_profile("vp-nope", "tenant-a")


def test_restart_reload_via_new_store(tmp_path) -> None:
    """A brand-new store instance over the same dir reloads the profile (4.7 restart)."""
    root = tmp_path / "voice_profiles"
    first = FilesystemVoiceProfileStore(root)
    profile = make_profile("vp-restart", "tenant-a", kind="cloned")
    first.save_profile(profile, {"k": "v"})

    second = FilesystemVoiceProfileStore(root)
    loaded, payload = second.load_profile("vp-restart", "tenant-a")
    assert loaded == profile
    assert payload == {"k": "v"}


def test_same_display_name_two_tenants_have_distinct_ids(
    store: FilesystemVoiceProfileStore,
) -> None:
    """Two tenants may use the same display name; ids stay distinct (4.8)."""
    a = make_profile(new_voice_profile_id(), "tenant-a", display_name="Giọng của tôi")
    b = make_profile(new_voice_profile_id(), "tenant-b", display_name="Giọng của tôi")
    store.save_profile(a, {"tenant": "a"})
    store.save_profile(b, {"tenant": "b"})
    assert a.voice_profile_id != b.voice_profile_id
    assert store.list_profiles("tenant-a") == [a]
    assert store.list_profiles("tenant-b") == [b]


def test_cross_tenant_get_does_not_leak(store: FilesystemVoiceProfileStore) -> None:
    """A profile stored under tenant A is invisible to tenant B (4.8)."""
    profile = make_profile("vp-a", "tenant-a")
    store.save_profile(profile, {})
    with pytest.raises(ProfileNotFoundError):
        store.load_profile("vp-a", "tenant-b")


def test_delete_one_tenant_does_not_affect_other(store: FilesystemVoiceProfileStore) -> None:
    a = make_profile("vp-a", "tenant-a")
    b = make_profile("vp-b", "tenant-b")
    store.save_profile(a, {})
    store.save_profile(b, {})
    store.delete_profile("vp-a", "tenant-a")
    with pytest.raises(ProfileNotFoundError):
        store.load_profile("vp-a", "tenant-a")
    loaded, _ = store.load_profile("vp-b", "tenant-b")
    assert loaded == b  # tenant B's profile untouched (4.9)


def test_list_profiles_returns_tenant_only(store: FilesystemVoiceProfileStore) -> None:
    store.save_profile(make_profile("vp-a1", "tenant-a"), {})
    store.save_profile(make_profile("vp-a2", "tenant-a"), {})
    store.save_profile(make_profile("vp-b1", "tenant-b"), {})
    assert [p.voice_profile_id for p in store.list_profiles("tenant-a")] == ["vp-a1", "vp-a2"]
    assert [p.voice_profile_id for p in store.list_profiles("tenant-b")] == ["vp-b1"]


def test_atomic_write_leaves_no_temp_files(store: FilesystemVoiceProfileStore, tmp_path) -> None:
    profile = make_profile("vp-1", "tenant-a")
    store.save_profile(profile, {"x": 1})
    files = list((tmp_path / "voice_profiles" / "tenant-a").iterdir())
    assert [f.name for f in files] == ["vp-1.json"]


def test_get_store_file_uri(tmp_path) -> None:
    store = get_store(f"file://{tmp_path / 'vp'}", tmp_path)
    assert isinstance(store, FilesystemVoiceProfileStore)


def test_get_store_unknown_scheme_fails(tmp_path) -> None:
    with pytest.raises(ValueError, match="unsupported voice store URI"):
        get_store("gcs://bucket", tmp_path)


def test_get_store_s3_requires_bucket(tmp_path) -> None:
    with pytest.raises(ValueError, match="bucket missing"):
        get_store("s3://", tmp_path)


def test_corrupted_stored_id_raises_not_found(store: FilesystemVoiceProfileStore) -> None:
    """A doc whose stored id mismatches its filename must fail clearly."""
    profile = make_profile("vp-1", "tenant-a")
    store.save_profile(profile, {})
    path = store._profile_path("tenant-a", "vp-1")
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["metadata"]["voice_profile_id"] = "vp-2"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(ProfileNotFoundError, match="corrupted"):
        store.load_profile("vp-1", "tenant-a")


# ── S3 store (task 4.4) — fake client, no boto3 required ──────────────────


def test_s3_round_trip_with_fake_client() -> None:
    client = FakeS3Client()
    s3 = S3VoiceProfileStore("voices-bucket", client=client)
    profile = make_profile("vp-s3", "tenant-a", kind="cloned")
    s3.save_profile(profile, {"schema_version": 1})
    loaded, payload = s3.load_profile("vp-s3", "tenant-a")
    assert loaded == profile
    assert payload == {"schema_version": 1}
    assert "tenant-a/vp-s3.json" in client.objects


def test_s3_missing_key_raises_not_found() -> None:
    s3 = S3VoiceProfileStore("voices-bucket", client=FakeS3Client())
    with pytest.raises(ProfileNotFoundError):
        s3.load_profile("vp-missing", "tenant-a")


def test_s3_delete_removes_object() -> None:
    client = FakeS3Client()
    s3 = S3VoiceProfileStore("voices-bucket", client=client)
    profile = make_profile("vp-s3", "tenant-a")
    s3.save_profile(profile, {})
    s3.delete_profile("vp-s3", "tenant-a")
    assert "tenant-a/vp-s3.json" not in client.objects
    with pytest.raises(ProfileNotFoundError):
        s3.load_profile("vp-s3", "tenant-a")


def test_s3_prefix_scopes_keys() -> None:
    client = FakeS3Client()
    s3 = S3VoiceProfileStore("voices-bucket", prefix="prod/tts", client=client)
    s3.save_profile(make_profile("vp-p", "tenant-a"), {})
    assert "prod/tts/tenant-a/vp-p.json" in client.objects
