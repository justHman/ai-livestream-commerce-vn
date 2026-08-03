from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLATFORM = ROOT / "services" / "platform"


def test_platform_roots_and_legacy_references() -> None:
    for name in ("livekit", "lmcache", "postgres", "redis"):
        assert (PLATFORM / name).is_dir()

    # livekit — pinned upstream image, fail-loud entrypoint, real smoke
    assert (PLATFORM / "livekit" / "Dockerfile").is_file()
    assert (PLATFORM / "livekit" / "livekit.yaml").is_file()
    assert (PLATFORM / "livekit" / "entrypoint.sh").is_file()
    assert (PLATFORM / "livekit" / "validate_config.py").is_file()
    assert (PLATFORM / "livekit" / "smoke.py").is_file()
    dockerfile = (PLATFORM / "livekit" / "Dockerfile").read_text()
    assert "@sha256:" in dockerfile, "livekit Dockerfile must pin an immutable digest"
    assert "|| true" not in dockerfile, "no ignored install/build failures"
    entrypoint = (PLATFORM / "livekit" / "entrypoint.sh").read_text()
    assert "livekit-server" in entrypoint, "entrypoint must exec real livekit-server"
    assert not any((PLATFORM / "livekit").rglob("metrics_app.py"))

    # lmcache — pinned upstream real runtime; synthetic metrics_app.py removed
    assert (PLATFORM / "lmcache" / "Dockerfile").is_file()
    assert (PLATFORM / "lmcache" / "entrypoint.sh").is_file()
    assert not (PLATFORM / "lmcache" / "metrics_app.py").exists()
    assert (PLATFORM / "lmcache" / "Dockerfile.dockerignore").is_file()
    lmcache_df = (PLATFORM / "lmcache" / "Dockerfile").read_text()
    assert "lmcache==0.5.2" in lmcache_df, "lmcache pin must be immutable"
    # Flag only best-effort install fallback patterns (|| true, || echo optional), not HEALTHCHECK fail-loudness.
    install_lines = [l for l in lmcache_df.splitlines() if "pip install" in l or "apt-get" in l or "apk add" in l]
    for line in install_lines:
        assert "||" not in line, f"install line must fail-loud, not fallback: {line.strip()}"
    lmcache_ep = (PLATFORM / "lmcache" / "entrypoint.sh").read_text()
    assert "lmcache server" in lmcache_ep
    assert "exec lmcache" in lmcache_ep, "must exec real upstream binary/CLI"

    # postgres/redis — docs + official local smoke, no src/sql/product
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
