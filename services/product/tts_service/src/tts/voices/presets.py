"""VieNeu v3 Turbo preset voice seeding (Change T task 5.5).

Preset profiles are metadata-only: the payload location is the placeholder
``preset://<name>`` and the provider adapter (cluster 4) resolves the actual
speaker embedding/reference codes from the SDK at synthesis time via
``get_preset_voice(name)``. Seeding needs no provider, only the static name
list — idempotent by construction (seed skips ids that already exist).
"""

from __future__ import annotations

from tts.voices.models import VoiceProfile, new_voice_profile_id
from tts.voices.store import DEFAULT_TENANT_ID

# Canonical v3 Turbo preset display names (vieneu/assets/voices_v3_turbo.json).
# The names are the payload location keys; the SDK payload itself resolves at
# provider init (cluster 4).
PRESET_VOICE_NAMES: tuple[str, ...] = (
    "Phạm Tuyên",
    "Lan Phương",
    "Minh Quân",
    "Thu Hà",
    "Hồng Đào",
    "Quang Huy",
    "Thu Minh",
    "Tuấn Anh",
    "Khánh Linh",
    "Ngọc Mai",
    "Bảo Châu",
    "Hải Yến",
    "Đức Thịnh",
    "Hà My",
)


def seed_preset_profiles(
    store: object, tenant_id: str = DEFAULT_TENANT_ID, *, provider_name: str, model_revision: str
) -> list[VoiceProfile]:
    """Create preset profiles for the tenant; skip ids that already exist.

    Returns the full tenant profile list after seeding (existing + new).
    Idempotent: re-seeding never duplicates (ids are content-addressed by name).
    """
    existing = {p.provider_payload_location for p in store.list_profiles(tenant_id)}
    for name in PRESET_VOICE_NAMES:
        location = f"preset://{name}"
        if location in existing:
            continue
        profile = VoiceProfile(
            voice_profile_id=new_voice_profile_id(),
            tenant_id=tenant_id,
            provider_name=provider_name,
            provider_model_revision=model_revision,
            profile_kind="preset",
            display_name=name,
            provider_payload_location=location,
        )
        store.save_profile(profile, {})
        existing.add(location)
    return store.list_profiles(tenant_id)
