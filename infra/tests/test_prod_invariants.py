from pathlib import Path

import re

ROOT = Path(__file__).resolve().parents[2]
PROD_VARS = ROOT / "infra" / "environments" / "prod" / "variables.tf"

IMAGE_VARS = ("image_backend", "image_llm", "image_tts", "image_lmcache", "image_avatar")


def _variable_block(source: str, name: str) -> str:
    match = re.search(rf'variable "{name}" \{{(.*?)\n\}}', source, re.DOTALL)
    assert match, f'variable "{name}" block not found'
    return match.group(0)


def test_session_store_defaults_to_redis_with_validation() -> None:
    block = _variable_block(PROD_VARS.read_text(encoding="utf-8"), "session_store")
    assert 'default     = "redis"' in block
    assert 'var.session_store == "redis"' in block


def test_desired_backend_defaults_to_two_with_minimum_validation() -> None:
    # terraform fmt aligns `default` after the longest key in the block, so match
    # the value fmt-agnostically rather than a fixed space count.
    block = _variable_block(PROD_VARS.read_text(encoding="utf-8"), "desired_backend")
    assert re.search(r"default\s*=\s*2\b", block)
    assert ">= 2" in block


def test_prod_image_variables_require_immutable_digests() -> None:
    source = PROD_VARS.read_text(encoding="utf-8")
    for name in IMAGE_VARS:
        block = _variable_block(source, name)
        assert "default" not in block, f"{name} must be required (no default)"
        assert ":latest" not in block, f"{name} must not default to a mutable :latest tag"
        assert "@sha256:" in block, f"{name} must validate an immutable digest regex"


def test_no_mutable_latest_tag_anywhere_in_prod_variables() -> None:
    assert ":latest" not in PROD_VARS.read_text(encoding="utf-8")
