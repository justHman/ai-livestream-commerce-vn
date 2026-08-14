"""Deterministic tests for the optional hybrid context-compression benchmark
(OpenSpec section 18).

Covers corpus parsing + provenance + schema version, hybrid context
classification, measurement-record shape/serialization, every scorer
(exact numbers/identifiers, diacritics, grounding, tool selection,
hallucination), the pure enablement gate (18.5/18.6), the end-to-end
simulation run with report output (JSON + Markdown NOT-PASS gate), and the
real-mode env-gate fail-loud. No network, no randomness.
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict

import pytest

from .context_compression_benchmark.benchmark_runner import (
    CORPUS_PATH,
    DEFAULT_MODEL_ID,
    DEFAULT_THRESHOLDS,
    REAL,
    REAL_RUNTIME_ENV,
    RUNNER_VERSION,
    SIMULATION,
    Answer,
    BenchmarkFixture,
    ContextChunk,
    GateResult,
    RunSummary,
    build_prompt,
    classify_context,
    default_thresholds,
    evaluate_gate,
    evaluate_run,
    load_context,
    load_fixtures,
    probe_runtime,
    run_benchmark,
    run_real,
    score_diacritics,
    score_exact,
    score_grounding,
    score_hallucination,
    score_tool_selection,
    set_real_seam,
    write_report,
)
from .context_compression_benchmark.benchmark_runner import _strip_diacritics

ALL_TASK_CLASSES = {
    "exact_number_or_identifier",
    "vietnamese_diacritics",
    "grounding",
    "tool_selection",
    "hallucination_prone",
}


def _fixture(fixture_id: str) -> BenchmarkFixture:
    return next(fixture for fixture in load_fixtures() if fixture.id == fixture_id)


def _chunks() -> list[ContextChunk]:
    return load_context()


def _summary(
    exact: float = 1.0,
    diacritics: float = 1.0,
    grounding: float = 1.0,
    tool: float = 1.0,
    hallucination: float = 0.0,
    tokens: int = 1000,
    latency: float = 100.0,
) -> RunSummary:
    return RunSummary(
        exact, diacritics, grounding, tool, hallucination, tokens, 50.0, latency, 0.01
    )


# -- 1. corpus parses + provenance truthful + schema version -------------------


def test_corpus_loads_with_expected_schema_and_provenance() -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    assert payload["schema"] == "vi-context-compression-corpus"
    assert payload["version"] == 1
    assert payload["provenance"]["authored_synthetic"] is True
    assert payload["provenance"]["contains_pii"] is False
    assert payload["provenance"]["factual_ground_truth"] is False


def test_corpus_version_mismatch_rejected(tmp_path) -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    payload["version"] = 999
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="schema/version mismatch"):
        load_fixtures(bad)


def test_corpus_provenance_mismatch_rejected(tmp_path) -> None:
    payload = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
    payload["provenance"] = {
        "authored_synthetic": False,
        "contains_pii": True,
        "factual_ground_truth": True,
    }
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="provenance"):
        load_fixtures(bad)


def test_every_task_class_has_fixtures() -> None:
    fixtures = load_fixtures()
    assert {fixture.task_class for fixture in fixtures} == ALL_TASK_CLASSES


def test_fixture_evidence_references_existing_context() -> None:
    fixtures = load_fixtures()
    chunk_ids = {chunk.id for chunk in load_context()}
    for fixture in fixtures:
        assert set(fixture.evidence) <= chunk_ids
        assert len(fixture.evidence) >= 1


# -- 2. hybrid classification (18.2) -------------------------------------------


def test_descriptive_kinds_are_image_eligible() -> None:
    for kind in ("long_description", "shop_story", "campaign_background"):
        assert classify_context(kind) == "descriptive"


def test_control_kinds_stay_text() -> None:
    for kind in ("exact_fact", "instruction", "tool_schema", "response_schema"):
        assert classify_context(kind) == "control"


def test_corpus_chunks_classify_descriptive() -> None:
    assert {chunk.classification for chunk in load_context()} == {"descriptive"}


def test_hybrid_prompt_keeps_control_text_and_marks_image() -> None:
    chunks = load_context()
    fixture = _fixture("fix-exact-001")
    hybrid = build_prompt(fixture, chunks, REAL)
    assert "[IMAGE:" in hybrid
    assert "desc-product-long" in hybrid
    baseline = build_prompt(fixture, chunks, SIMULATION)
    assert "[IMAGE:" not in baseline
    # All-text baseline carries the descriptive content inline (18.1).
    descriptive = next(chunk.text for chunk in chunks if chunk.id == "desc-product-long")
    assert descriptive in baseline
    # Control header + question stay text in both modes.
    for prompt in (baseline, hybrid):
        assert fixture.question in prompt
        assert "Bạn là trợ lý bán hàng" in prompt


# -- 3. measurement record shape + serialization (18.3) -------------------------


def test_answer_record_shape_and_serialization() -> None:
    chunks = load_context()
    answer = evaluate_run(_fixture("fix-exact-001"), chunks, SIMULATION)
    payload = json.loads(json.dumps(asdict(answer)))
    assert payload["fixture_id"] == "fix-exact-001"
    assert payload["mode"] == SIMULATION
    assert payload["reported_input_tokens"] > 0
    assert payload["ttft_ms"] > 0
    assert payload["total_latency_ms"] > 0
    assert payload["cost"] >= 0
    assert isinstance(answer, Answer)


def test_hybrid_measurements_reduce_tokens_and_latency() -> None:
    chunks = load_context()
    baseline = evaluate_run(_fixture("fix-exact-001"), chunks, SIMULATION)
    hybrid = evaluate_run(_fixture("fix-exact-001"), chunks, REAL)
    assert hybrid.reported_input_tokens < baseline.reported_input_tokens
    assert hybrid.total_latency_ms < baseline.total_latency_ms


# -- 4. scorers (18.4) -----------------------------------------------------------


def test_exact_number_accuracy() -> None:
    assert score_exact("12", "12") is True
    assert score_exact(" 12 ", "12") is True
    assert score_exact("13", "12") is False


def test_exact_identifier_accuracy() -> None:
    assert score_exact("SKU-NW-2247", "SKU-NW-2247") is True
    assert score_exact("sku-nw-2247", "SKU-NW-2247") is True
    assert score_exact("SKU-NW-2248", "SKU-NW-2247") is False


def test_diacritic_preservation_required() -> None:
    # "được 250.000đ" with diacritics must pass; the diacritic-stripped
    # "duoc 250.000d" must fail even though it has the same wording.
    assert score_diacritics("được 250.000đ", "được 250.000đ") is True
    assert score_diacritics("duoc 250.000d", "được 250.000đ") is False
    assert score_diacritics("Được 250.000đ", "được 250.000đ") is True


def test_diacritic_strip_helper_removes_vietnamese_marks() -> None:
    assert _strip_diacritics("được 250.000đ") == "duoc 250.000d"


def test_grounding_answer_cites_only_evidence() -> None:
    evidence = "Chiếc túi được làm từ vải canvas dày."
    assert score_grounding("Vải canvas dày", "Vải canvas dày", evidence) is True
    assert score_grounding("Vải canvas dày, màu đỏ", "Vải canvas dày", evidence) is False
    assert score_grounding("Vải denim", "Vải canvas dày", evidence) is False


def test_tool_selection_accuracy() -> None:
    assert score_tool_selection("promotion_lookup", "promotion_lookup") is True
    assert score_tool_selection("order_status_lookup", "promotion_lookup") is False


def test_hallucination_detection() -> None:
    evidence = "Chiếc túi được làm từ vải canvas dày."
    # Refusal ("no information") is never a hallucination.
    assert score_hallucination("Không có thông tin", "Không có thông tin", evidence) is False
    # Asserting a fact absent from the evidence is a hallucination.
    assert score_hallucination("Túi có màu đỏ", "Không có thông tin", evidence) is True
    assert score_hallucination("Vải canvas dày", "Vải canvas dày", evidence) is False


def test_hallucination_prone_fixtures_answer_refusal() -> None:
    chunks = load_context()
    for fixture in load_fixtures():
        if fixture.task_class != "hallucination_prone":
            continue
        answer = evaluate_run(fixture, chunks, SIMULATION)
        assert answer.hallucination is False
        assert answer.exact is True


# -- 5. gate (18.5/18.6) ----------------------------------------------------------


def test_gate_enabled_when_all_thresholds_met() -> None:
    baseline = _summary()
    hybrid = _summary(tokens=500, latency=60.0)  # 50% token + 40% latency cut
    result = evaluate_gate(baseline, hybrid, default_thresholds())
    assert result.enabled is True
    assert result.reasons == ()


def test_gate_disabled_on_accuracy_loss() -> None:
    baseline = _summary()
    hybrid = _summary(exact=0.8, tokens=500, latency=60.0)
    result = evaluate_gate(baseline, hybrid, default_thresholds())
    assert result.enabled is False
    assert any("exact accuracy" in reason for reason in result.reasons)


def test_gate_disabled_without_material_token_benefit() -> None:
    baseline = _summary()
    hybrid = _summary(tokens=990, latency=60.0)  # only 1% token reduction
    result = evaluate_gate(baseline, hybrid, default_thresholds())
    assert result.enabled is False
    assert any("token" in reason for reason in result.reasons)


def test_gate_disabled_without_material_latency_benefit() -> None:
    baseline = _summary()
    hybrid = _summary(tokens=500, latency=99.0)  # only 1% latency reduction
    result = evaluate_gate(baseline, hybrid, default_thresholds())
    assert result.enabled is False
    assert any("latency" in reason for reason in result.reasons)


def test_gate_disabled_on_hallucination_regression() -> None:
    baseline = _summary()
    hybrid = _summary(hallucination=0.3, tokens=500, latency=60.0)
    result = evaluate_gate(baseline, hybrid, default_thresholds())
    assert result.enabled is False
    assert any("hallucination" in reason for reason in result.reasons)


def test_gate_reasons_are_typed_and_ordered() -> None:
    baseline = _summary()
    hybrid = _summary(exact=0.7, tokens=990, latency=99.0)
    result = evaluate_gate(baseline, hybrid, default_thresholds())
    assert isinstance(result, GateResult)
    assert isinstance(result.reasons, tuple)
    assert len(result.reasons) >= 2


def test_thresholds_defaults_are_typed() -> None:
    thresholds = default_thresholds()
    assert thresholds == DEFAULT_THRESHOLDS
    assert thresholds["exact_delta_min"] == -0.05
    assert thresholds["token_reduction_min"] == 0.10
    assert thresholds["latency_reduction_min"] == 0.10


def test_simulation_benchmark_passes_its_own_gate() -> None:
    baseline = run_benchmark(SIMULATION)
    hybrid = run_benchmark(REAL)
    result = evaluate_gate(baseline.summary, hybrid.summary, default_thresholds())
    assert result.enabled is True
    assert result.reasons == ()


# -- 6. simulation mode end-to-end + report -------------------------------------


def test_run_benchmark_simulation_shape() -> None:
    result = run_benchmark(SIMULATION)
    assert result.meta["runner_version"] == RUNNER_VERSION
    assert result.meta["runtime_mode"] == SIMULATION
    assert result.meta["model_id"] == DEFAULT_MODEL_ID
    assert len(result.answers) == len(load_fixtures())
    assert result.summary.exact_accuracy == 1.0
    assert result.summary.diacritics_accuracy == 1.0
    assert result.summary.grounding_accuracy == 1.0
    assert result.summary.tool_selection_accuracy == 1.0
    assert result.summary.hallucination_rate == 0.0
    assert result.summary.total_input_tokens > 0


def test_report_roundtrip_with_not_pass_gate(tmp_path) -> None:
    baseline = run_benchmark(SIMULATION)
    hybrid = run_benchmark(REAL)
    json_path, md_path = write_report(baseline, hybrid, default_thresholds(), tmp_path)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["baseline"]["summary"]["total_input_tokens"] > 0
    assert payload["hybrid"]["summary"]["total_input_tokens"] > 0
    assert payload["gate"]["enabled"] is True

    md = md_path.read_text(encoding="utf-8")
    assert "NOT PASS" in md  # simulation mode -> no real-model evidence
    assert "Baseline (all-text)" in md
    assert "Hybrid" in md


def test_report_json_gate_disabled_when_hybrid_weak(tmp_path) -> None:
    baseline = run_benchmark(SIMULATION)
    hybrid = run_benchmark(SIMULATION)  # same accuracy, no benefit at all
    json_path, _ = write_report(baseline, hybrid, default_thresholds(), tmp_path)
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["gate"]["enabled"] is False


def test_probe_runtime_reports_no_seam() -> None:
    report = probe_runtime()
    assert set(report) == {"seam_available", "model_id", "detail"}
    assert report["model_id"] == DEFAULT_MODEL_ID
    assert isinstance(report["seam_available"], bool)


# -- 7. real mode requires the env gate ------------------------------------------


def test_run_real_requires_env_gate(monkeypatch) -> None:
    monkeypatch.delenv(REAL_RUNTIME_ENV, raising=False)
    with pytest.raises(RuntimeError, match=REAL_RUNTIME_ENV):
        run_real()


def test_run_real_fail_loud_without_seam(monkeypatch) -> None:
    monkeypatch.setenv(REAL_RUNTIME_ENV, "1")
    monkeypatch.delenv("CC_BENCH_MODEL", raising=False)
    # No seam registered (module state may be shared across tests).
    if probe_runtime()["seam_available"]:
        pytest.skip("a real seam is registered; the gate may pass on this machine")
    with pytest.raises(RuntimeError, match="no model seam"):
        run_real()


# -- helpers ----------------------------------------------------------------------


def test_ground_truth_fixtures_have_nonempty_questions() -> None:
    for fixture in load_fixtures():
        assert fixture.question.strip()
        assert fixture.answer.strip()


def test_ascii_only_tokens_are_skipped_by_grounding() -> None:
    # Tool names and short tokens never trigger absent-fact citations.
    assert score_grounding("promotion_lookup", "promotion_lookup", "") is True


def test_grounding_accepts_restated_fact() -> None:
    evidence = "Chiếc túi được làm từ vải canvas dày."
    assert score_grounding("Túi làm bằng vải canvas dày", "Vải canvas dày", evidence) is True


def test_diacritics_roundtrip_uses_unicode_nfd() -> None:
    assert unicodedata.is_normalized("NFD", "được") is False
    assert _strip_diacritics("được") == "duoc"


def test_build_prompt_raises_on_unknown_mode() -> None:
    fixture = _fixture("fix-exact-001")
    with pytest.raises(ValueError, match="unknown mode"):
        build_prompt(fixture, _chunks(), "bogus")


class _FakeSeam:
    """A registered seam that must never be called by the harness itself."""

    def run_text(self, prompt: str) -> dict:
        raise AssertionError("real seam must not be invoked in CI tests")


def test_set_real_seam_registers_operator_model() -> None:
    set_real_seam(_FakeSeam())
    try:
        assert probe_runtime()["seam_available"] is True
    finally:
        set_real_seam(None)
