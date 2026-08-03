"""Tests for OpenSpec 2.2 validated workflow inputs."""

from pathlib import Path

import pytest

from scripts.ci.validate_workflow_inputs import (
    PRODUCT_SERVICE_IDS,
    SERVICE_SHORT,
    SUPPORTED_SERVICES,
    emit_github_json_output,
    validate_environment,
    validate_services,
    validate_sha,
    validate_service_tag,
    main as cli_main,
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
    with pytest.raises(ValueError, match="does not resolve"):
        validate_sha("0" * 40, ROOT)


# ── Environment ─────────────────────────────────────────────────────────────


def test_valid_environment():
    assert validate_environment("dev") == "dev"
    assert validate_environment("staging") == "staging"
    assert validate_environment("prod") == "prod"


def test_environment_explicit_allowlist():
    # Per-workflow allowlist: production environment may only be "prod".
    assert validate_environment("prod", allowed={"prod"}) == "prod"
    with pytest.raises(ValueError, match="Unsupported"):
        validate_environment("dev", allowed={"prod"})


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


# ── Services (canonical identifier vocabulary) ──────────────────────────────


def test_canonical_service_ids_shaped():
    # Design §4 dispatch contract uses backend_service/llm_service/... identifiers.
    assert PRODUCT_SERVICE_IDS == frozenset(
        {"backend_service", "llm_service", "tts_service", "avatar_service"}
    )
    assert SERVICE_SHORT["backend_service"] == "backend"


def test_valid_canonical_services():
    assert validate_services("tts_service,backend_service") == [
        "backend_service",
        "tts_service",
    ]


def test_short_service_id_rejected():
    # Dispatch inputs must be canonical (<short>_service); short "backend" is unknown.
    with pytest.raises(ValueError, match="Unknown service"):
        validate_services("backend")


def test_all_supported_subset():
    assert set(validate_services(",".join(sorted(SUPPORTED_SERVICES)))) == SUPPORTED_SERVICES


def test_per_workflow_allowlist_bounds():
    # A deploy workflow may allow only {backend_service, tts_service}.
    allowed = {"backend_service", "tts_service"}
    assert validate_services("tts_service,backend_service", allowed=allowed) == [
        "backend_service",
        "tts_service",
    ]
    with pytest.raises(ValueError, match="Unknown service"):
        validate_services("avatar_service", allowed=allowed)


def test_empty_services_rejected():
    with pytest.raises(ValueError, match="empty"):
        validate_services("")


def test_whitespace_only_rejected():
    with pytest.raises(ValueError, match="empty"):
        validate_services("   ")


def test_unknown_service_rejected():
    with pytest.raises(ValueError, match="Unknown service"):
        validate_services("database_service")


def test_case_variant_rejected():
    with pytest.raises(ValueError, match="lowercase"):
        validate_services("Backend_service,tts_service")


def test_injection_input_rejected():
    with pytest.raises(ValueError, match="Unknown service"):
        validate_services("backend_service; rm -rf /")


def test_shell_metachar_rejected():
    with pytest.raises(ValueError):
        validate_services("backend_service,$(id)")


def test_consecutive_commas_rejected():
    with pytest.raises(ValueError, match="Empty entry"):
        validate_services("backend_service,,tts_service")


def test_dedup_deterministic():
    assert validate_services("avatar_service,backend_service,tts_service") == [
        "avatar_service",
        "backend_service",
        "tts_service",
    ]
    assert validate_services("tts_service,avatar_service,backend_service") == [
        "avatar_service",
        "backend_service",
        "tts_service",
    ]


# ── Service tags ────────────────────────────────────────────────────────────


def test_valid_service_tag_returns_canonical():
    r = validate_service_tag("backend-v1.2.3")
    assert r["service"] == "backend_service"
    assert r["service_short"] == "backend"
    assert r["version"] == "v1.2.3"


def test_valid_platform_image_tag():
    assert validate_service_tag("tts-v0.5.0")["service"] == "tts_service"


def test_bad_service_tag_prefix():
    with pytest.raises(ValueError, match="unsupported service"):
        validate_service_tag("database-v1.2.3")


def test_non_semver_tag():
    with pytest.raises(ValueError, match="does not match"):
        validate_service_tag("backend-v1.2")


def test_unsupported_service_name_in_tag():
    with pytest.raises(ValueError, match="unsupported service"):
        validate_service_tag("lmcache-v1.2.3")


def test_empty_tag():
    with pytest.raises(ValueError, match="empty"):
        validate_service_tag("")


# ── GITHUB_OUTPUT ───────────────────────────────────────────────────────────


def test_emit_github_json_output(tmp_path, monkeypatch):
    out_file = tmp_path / "output.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_file))
    emit_github_json_output("services_matrix", ["backend_service", "tts_service"])
    content = out_file.read_text()
    assert "services_matrix<<EOF" in content
    assert '"backend_service"' in content
    assert "EOF" in content


# ── CLI ─────────────────────────────────────────────────────────────────────


def _run_cli(args):
    import subprocess

    return subprocess.run(
        ["python", "scripts/ci/validate_workflow_inputs.py", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_cli_requires_all_inputs():
    proc = _run_cli([])
    assert proc.returncode == 1
    assert "Missing required input: --sha" in proc.stderr
    assert "Missing required input: --env" in proc.stderr
    assert "Missing required input: --services" in proc.stderr


def test_cli_requires_key_inputs():
    proc = _run_cli(["--sha", HEAD_SHA, "--env", "dev"])
    assert proc.returncode == 1
    assert "Missing required input: --services" in proc.stderr
    assert "--sha" not in proc.stderr


def test_cli_partial_require_ok():
    proc = _run_cli(
        ["--sha", HEAD_SHA, "--env", "dev", "--require", "sha,env", "--services", "tts_service"]
    )
    assert proc.returncode == 0


def test_cli_rejects_missing_github_output_without_inputs(capsys):
    with pytest.raises(SystemExit) as exc:
        cli_main(["--github-output", "--sha", HEAD_SHA, "--env", "dev"])
    assert exc.value.code == 1
    out = capsys.readouterr()
    assert "Missing required input: --services" in out.err


def test_cli_unknown_require_value():
    proc = _run_cli(["--sha", HEAD_SHA, "--require", "bogus"])
    assert proc.returncode == 2
    assert "unknown --require" in proc.stderr
