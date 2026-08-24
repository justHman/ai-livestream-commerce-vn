"""R8.7: shared executable/resource files fan out to EVERY real consumer lane."""

from scripts.ci.detect_affected_areas import classify_path, detect_affected_areas

# Table-driven expectations: a change under each shared path MUST reach every
# consumer lane listed. Derived from actual build consumers (Dockerfile COPY)
# and the R8.7 shared-area list.
FANOUT_TABLE = [
    # scripts/model_assets/* is COPYed into the llm/tts/avatar images only.
    ("scripts/model_assets/fetch_weights.sh", {"llm_service", "tts_service", "avatar_service"}),
    ("scripts/model_assets/upload.py", {"llm_service", "tts_service", "avatar_service"}),
    ("scripts/model_assets/", {"llm_service", "tts_service", "avatar_service"}),
    # Backend runtime resources are consumed by the backend image/lane.
    ("services/product/backend_service/resources/skills/plan.yaml", {"backend_service"}),
    ("services/product/backend_service/resources/profanity/list.txt", {"backend_service"}),
    # Workflow helpers always trigger the repo-tools lane.
    ("scripts/ci/detect_changes.py", {"ci"}),
    # Existing contract fan-out (owner + consumer) stays intact.
    ("services/product/tts_service/contracts/tts.yaml", {"tts_service", "backend_service"}),
]


def test_fanout_table():
    for path, expected in FANOUT_TABLE:
        got = set(classify_path(path))
        assert expected <= got, f"{path}: classified {sorted(got)} missing {sorted(expected - got)}"


def test_model_assets_affects_every_image_lane():
    res = detect_affected_areas(["scripts/model_assets/fetch_weights.sh"])
    assert {"llm_service", "tts_service", "avatar_service"} <= set(res["areas"]), res["areas"]


def test_backend_resources_affect_backend_lane():
    res = detect_affected_areas(
        ["services/product/backend_service/resources/skills/plan.yaml"]
    )
    assert "backend_service" in res["areas"]
