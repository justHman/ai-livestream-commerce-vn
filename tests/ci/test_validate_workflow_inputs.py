"""Tests for OpenSpec 2.2 validated workflow inputs."""

from pathlib import Path

import pytest

from scripts.ci.validate_workflow_inputs import (
    SUPPORTED_SERVICES,
    validate_sha,
    validate_environment,
    validate_services,
    validate_service_tag,
    emit_github_json_output,
)

ROOT = Path(__file__).resolve().parents[2]
HEAD_SHA = "ac0ff9c26aa9aeb88bb08e9dec627de819f37812"


# ── SHA ─────────────────────────────────────────────────────────────────────


def test_valid_sha_resolves():
    assert validate_sha(HEAD_SHA, ROOT) == HEAD_SHA


def test_upper_hex_normalized():
    assert validate_sha(HEAD_SHA.upper(), ROOT) == HEAD_SHA


def test_short_sha_rejected():
    with pytest.raises(ValueError, match="40-character hexadecimal"):
        validate_sha(HEAD_SHA[:10], ROOT)


def test_non_hex_rejected():
    with pytest.raises(ValueError, match="hex"):
        validate_sha("z" * 40, ROOT)


def test_sha_with_whitespace_rejected():
    with pytest.raises(ValueError):
        validate_sha(" " + HEAD_SHA, ROOT)


def test_unknown_sha_rejected():
    # All zeros is a valid 40-hex string but does not resolve in the repo.
    with pytest.raises(ValueError, match="does not resolve"):
        validate_sha("0" * 40, ROOT)


# ── Environment ─────────────────────────────────────────────────────────────


def test_valid_environment():
    assert validate_environment("dev") == "dev"
    assert validate_environment("staging") == "staging"
    assert validate_environment("prod") == "prod"


def test_empty_environment_rejected():
    with pytest.raises(ValueError, match="empty"):
        validate_environment("")


def test_unsupported_environment_rejected():
    with pytest.raises(ValueError, match="Unsupported"):
        validate_environment("qa")


def test_uppercase_environment_rejected():
    with pytest.raises(ValueError, match="lowercase"):
        validate_environment("DEV")


def test_symbols_in_environment_rejected():
    with pytest.raises(ValueError, match="lowercase"):
        validate_environment("dev!")


# ── Services ────────────────────────────────────────────────────────────────


def test_valid_services_deduped_sorted():
    assert validate_services("tts,backend,tts") == ["backend", "tts"]


def test_valid_platform_services():
    assert validate_services("livekit,lmcache") == ["livekit", "lmcache"]


def test_all_supported_subset():
    assert set(validate_services(",".join(sorted(SUPPORTED_SERVICES)))) == SUPPORTED_SERVICES


def test_empty_services_rejected():
    with pytest.raises(ValueError, match="empty"):
        validate_services("")


def test_whitespace_only_rejected():
    with pytest.raises(ValueError, match="empty"):
        validate_services("   ")


def test_unknown_service_rejected():
    with pytest.raises(ValueError, match="Unknown service"):
        validate_services("database")


def test_case_variant_rejected():
    with pytest.raises(ValueError, match="lowercase"):
        validate_services("Backend,tts")


def test_injection_input_rejected():
    with pytest.raises(ValueError, match="Unknown service"):
        validate_services("backend; rm -rf /")


def test_shell_metachar_rejected():
    with pytest.raises(ValueError):
        validate_services("backend,$(id)")


def test_consecutive_commas_rejected():
    with pytest.raises(ValueError, match="Empty entry"):
        validate_services("backend,,tts")


def test_dedup_deterministic():
    assert validate_services("avatar,backend,tts") == ["avatar", "backend", "tts"]
    assert validate_services("tts,avatar,backend") == ["avatar", "backend", "tts"]


# ── Service tags ────────────────────────────────────────────────────────────


def test_valid_service_tag():
    r = validate_service_tag("backend-v1.2.3")
    assert r["service"] == "backend"
    assert r["version"] == "v1.2.3"


def test_platform_service_tag():
    assert validate_service_tag("livekit-v0.5.0")["service"] == "livekit"


def test_bad_service_tag_prefix():
    with pytest.raises(ValueError, match="does not match"):
        validate_service_tag("database-v1.2.3")


def test_non_semver_tag():
    with pytest.raises(ValueError, match="does not match"):
        validate_service_tag("backend-v1.2")


def test_empty_tag():
    with pytest.raises(ValueError, match="empty"):
        validate_service_tag("")


# ── GITHUB_OUTPUT ───────────────────────────────────────────────────────────


def test_emit_github_json_output(tmp_path, monkeypatch):
    out_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    emit_github_json_output("services_matrix", ["backend", "tts"])
    content = out_file.read_text()
    assert "services_matrix<<EOF" in content
    assert '"backend"' in content
    assert "EOF" in content
