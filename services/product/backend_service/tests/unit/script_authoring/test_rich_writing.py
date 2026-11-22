"""Call-time rich-writing opt-in preserves the original prompt and strict gate by default."""

from pathlib import Path

import pytest

from backend.application.script_authoring.gate.context import (
    ScriptGateContext,
    rich_writing_enabled,
)
from backend.application.script_authoring.gate.results import Severity
from backend.application.script_authoring.gate.rules.format import check_punctuation
from backend.application.script_authoring.gate.rules.repetition import check_local_repetition
from backend.application.script_authoring.generation import ordered_units as units
from backend.config import ScriptAuthoringConfig


def _highlight_prompt(seconds: float = 28) -> str:
    product = units.ProductBrief("p", "Túi vải", claims=("Có quai xách.",))
    specs = [units.UnitSpec("highlight", claims=product.claims, target_seconds=seconds)]
    return units.build_unit_prompt(units.SessionBrief(title="Demo"), product, specs, 0)


@pytest.mark.parametrize("value", [None, "", "0", "false", "no", "off"])
def test_flag_off_preserves_original_prompt_and_lengths(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("SCRIPT_RICH_WRITING_ENABLED", raising=False)
    else:
        monkeypatch.setenv("SCRIPT_RICH_WRITING_ENABLED", value)
    assert not rich_writing_enabled()
    prompt = _highlight_prompt()
    # Frozen from the original prompt before the rich-writing change.
    original = Path(__file__).with_name("fixtures") / "rich_writing_off_highlight.txt"
    assert prompt.encode("utf-8") == original.read_bytes()
    assert "1 đến 2 câu" in prompt
    assert "không kéo dài" in prompt
    assert units.product_target_s(360, 10) == 250
    assert units.role_bounds_s("highlight") == (1.0, 60.0)


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", " TRUE ", "YeS", "ON"])
def test_rich_prompt_and_lengths_are_read_at_call_time(monkeypatch, value):
    monkeypatch.delenv("SCRIPT_RICH_WRITING_ENABLED", raising=False)
    specs = [units.UnitSpec("highlight", target_seconds=100)]
    original_tokens = units.unit_max_tokens(specs)
    monkeypatch.setenv("SCRIPT_RICH_WRITING_ENABLED", value)
    assert rich_writing_enabled()
    prompt = _highlight_prompt(100)
    assert "mỗi điểm nói 2 đến 4 câu" in prompt
    assert "cả phần khoảng 5 đến 9 câu" in prompt
    assert (
        "Độ dài mục tiêu: khoảng 1360 đến 1600 ký tự. Hãy nói đủ độ dài này bằng "
        "cách giải thích kỹ từng ý đã cho, tương tác với khán giả và chuyển ý mượt; "
        "tuyệt đối không thêm sự thật, con số, ưu đãi hay cam kết mới."
    ) in prompt
    assert "TUYỆT ĐỐI KHÔNG thêm" in prompt
    # All strict factual prohibitions, number/price rules, injection and time rules survive.
    strict_suffix = units._TRUTH_RULES.split("TUYỆT ĐỐI KHÔNG thêm:", 1)[1]
    assert strict_suffix in prompt
    assert units.product_target_s(360, 10) == 360
    assert units.product_floor_s(360, 1) == 150
    assert units.product_floor_s(360, 6) == 180
    assert units.role_bounds_s("highlight") == (1.0, 100.0)
    assert units.unit_max_tokens(specs) > original_tokens
    monkeypatch.delenv("SCRIPT_RICH_WRITING_ENABLED")
    assert units.product_target_s(360, 10) == 250
    assert units.unit_max_tokens(specs) == original_tokens


@pytest.mark.parametrize(
    ("role", "maximum"),
    [
        ("opening", 40),
        ("intro", 60),
        ("highlight", 100),
        ("sizes", 100),
        ("assurance", 100),
        ("offer", 100),
        ("closing", 35),
    ],
)
def test_rich_role_bounds(monkeypatch, role, maximum):
    monkeypatch.setenv("SCRIPT_RICH_WRITING_ENABLED", "1")
    assert units.role_bounds_s(role) == (1.0, maximum)


@pytest.mark.parametrize("enabled", [False, True])
def test_local_repetition_and_punctuation_severity(monkeypatch, enabled):
    monkeypatch.setenv("SCRIPT_RICH_WRITING_ENABLED", "1" if enabled else "0")
    context = ScriptGateContext()
    text = "Có quai xách dài. Có quai xách dài. Có quai xách dài."
    repetition = check_local_repetition(text, context)
    punctuation = check_punctuation("Mọi người xem nhé!!", context)
    expected = Severity.WARNING if enabled else Severity.ERROR
    assert repetition and punctuation
    assert all(v.rule_id == "REPETITION_LOCAL" and v.severity == expected for v in repetition)
    assert all(v.rule_id == "FORMAT_PUNCTUATION" and v.severity == expected for v in punctuation)


def test_config_reads_unit_timeouts(monkeypatch):
    monkeypatch.setenv("SA_UNIT_LLM_CALL_TIMEOUT_S", "125.5")
    monkeypatch.setenv("SA_UNIT_JOB_DEADLINE_S", "1800.5")
    config = ScriptAuthoringConfig.from_env()
    assert config.unit_llm_call_timeout_s == 125.5
    assert config.unit_job_deadline_s == 1800.5


def test_config_keeps_unit_timeout_defaults(monkeypatch):
    monkeypatch.delenv("SA_UNIT_LLM_CALL_TIMEOUT_S", raising=False)
    monkeypatch.delenv("SA_UNIT_JOB_DEADLINE_S", raising=False)
    config = ScriptAuthoringConfig.from_env()
    assert config.unit_llm_call_timeout_s == 60.0
    assert config.unit_job_deadline_s == 900.0
