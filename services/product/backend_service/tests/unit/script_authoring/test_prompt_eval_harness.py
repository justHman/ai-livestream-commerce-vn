"""The offline prompt-eval harness runs without a model and applies its expectations."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[2] / "prompt_eval"


@pytest.fixture(scope="module")
def harness():
    spec = importlib.util.spec_from_file_location("run_eval", EVAL_DIR / "run_eval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _case(case_id: str) -> dict:
    return next(
        c
        for c in json.loads((EVAL_DIR / "corpus.json").read_text(encoding="utf8"))
        if c["id"] == case_id
    )


def test_corpus_has_the_eight_planned_cases():
    ids = [c["id"][:2] for c in json.loads((EVAL_DIR / "corpus.json").read_text(encoding="utf8"))]
    assert ids == [f"T{n}" for n in range(1, 9)]


def test_fake_run_finishes_and_returns_ok(harness, capsys):
    assert harness.main(["--fake", "--runs", "1"]) == 0


def test_a_forbidden_phrase_in_the_script_is_reported(harness):
    case = {"expect": {"forbid": ["chữa"]}}
    assert harness.expect_lines(case, None, {"texts": ["Nó chữa bệnh."], "prompts": []}) == [
        "FAIL absent 'chữa'"
    ]


def test_contradicting_claims_and_a_free_discount_never_reach_the_writer_prompt(harness):
    case = _case("T4-contradiction-and-free")
    writer = harness.run_writer(case, tuple(case["claims"]), harness.fake_llm)
    prompts = " ".join(writer["prompts"]).lower()
    assert "đổi trả" not in prompts and "100%" not in prompts
