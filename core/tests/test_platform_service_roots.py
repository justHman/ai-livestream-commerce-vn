from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "services" / "platform"


def test_platform_roots_and_legacy_references() -> None:
    for name in ("livekit", "lmcache", "postgres", "redis"):
        assert (PLATFORM / name).is_dir()

    assert (PLATFORM / "livekit" / "Dockerfile").is_file()
    assert (PLATFORM / "livekit" / "livekit.yaml").is_file()
    assert (PLATFORM / "lmcache" / "Dockerfile").is_file()
    assert (PLATFORM / "lmcache" / "metrics_app.py").is_file()
    assert (PLATFORM / "postgres" / "README.md").is_file()
    assert (PLATFORM / "redis" / "README.md").is_file()
    assert not list((PLATFORM / "postgres").glob("smoke.*"))
    assert not list((PLATFORM / "redis").glob("smoke.*"))
    assert not (PLATFORM / "redis" / "redis.conf").exists()
    assert not any((PLATFORM / name / "src").exists() for name in ("livekit", "lmcache", "postgres", "redis"))
    assert not list(PLATFORM.rglob("*.sql"))

    legacy = ROOT / "services"
    assert not (legacy / "livekit").exists()
    assert not (legacy / "lmcache").exists()

    refs = [
        ROOT / ".github" / "workflows" / "build-images.yml",
        ROOT / ".github" / "workflows" / "deploy-dev.yml",
        ROOT / "services" / "README.md",
        ROOT / "docs" / "runbook-deploy-prep.md",
    ]
    for path in refs:
        text = path.read_text()
        assert "services/livekit/" not in text
        assert "services/lmcache/" not in text

    assert (ROOT / "core" / "sql" / "runtime_schema.sql").is_file()
