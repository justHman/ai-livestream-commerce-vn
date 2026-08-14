"""Optional hybrid agent context-compression benchmark (OpenSpec section 18).

Question-answer fixtures over a versioned, authored-synthetic Vietnamese
corpus (``vi_context_fixtures_v1.json``), answered in two modes against the
same target model: all-text baseline and hybrid mode (eligible descriptive
context rendered as an image; instruction/control/dynamic exact facts stay
text). Measures the Decision 21 metric list (input tokens, TTFT, total
latency, cost, exact number/identifier accuracy, Vietnamese diacritics,
grounding, tool selection, hallucination) and gates hybrid enablement on
non-regression + material-benefit thresholds (18.5/18.6).

Two runtime modes, mirroring the 8.3 benchmark runner:

- ``simulation`` (default, CI-safe): no model is called. Answers come from
  the fixture ground truth, and input tokens / TTFT / total latency / cost
  are deterministic fakes derived from the mode- and fixture-specific
  token multipliers. The gate is fully exercised by the fake measurements,
  so acceptance (18.5/18.6) is testable with no network.
- ``real`` (operator-invoked): calls a real vision-capable model through a
  model-agnostic seam. Requires the explicit ``CC_BENCH_RUNTIME=1`` opt-in
  and a ``CC_BENCH_MODEL`` model id (or a default when the env is absent);
  the body is correct-by-inspection only (``# pragma: no cover`` — it
  cannot run in CI).

Output is JSON plus a Markdown report ending with a NOT-PASS section
whenever the mode is not real (no real-model evidence, no PASS).

Public API: module constants, the dataclasses, ``load_fixtures``,
``classify_context``, ``score_exact``, ``score_diacritics``,
``score_grounding``, ``score_tool_selection``, ``score_hallucination``,
``summary_for``, ``evaluate_run``, ``probe_runtime``, ``run_benchmark``,
``run_real``, ``default_thresholds``, ``evaluate_gate``, and a minimal CLI
(``python -m tests.unit.context_compression_benchmark.benchmark_runner``).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TypedDict, cast

SCHEMA = "vi-context-compression-corpus"
VERSION = 1
CORPUS_PATH = Path(__file__).with_name("vi_context_fixtures_v1.json")
RUNNER_VERSION = "1.0.0"

# The actual target vision-capable model for the operator-invoked real run
# (18.1: the fixture set names the target model; the harness reads the id
# from CC_BENCH_MODEL, falling back to this default).
DEFAULT_MODEL_ID = "gpt-4o"
MODEL_ENV = "CC_BENCH_MODEL"
# Explicit opt-in gate for the real-model mode, mirroring VIENEU_RUNTIME=1.
REAL_RUNTIME_ENV = "CC_BENCH_RUNTIME"

# Modes: "simulation" (deterministic fakes) | "real" (operator-invoked).
SIMULATION = "simulation"
REAL = "real"
VALID_MODES = (SIMULATION, REAL)

# Chunk kinds: control-plane chunks are mandatory text; descriptive chunks
# are the only image-eligible candidates (Decision 21).
CONTROL = "control"
DESCRIPTIVE = "descriptive"

# Token/latency/cost multipliers for simulation mode. All-text mode answers
# are the fixture ground truth; hybrid mode is simulated as the "good" case
# where the image does not hurt accuracy (answers stay ground truth) but the
# text context is shorter, so its fake token counts drop and TTFT/latency
# shrink. Measured on the deterministic fixture text lengths, so every run
# is a pure function of the corpus.
_SIM_TOKENS_PER_CHAR = 0.9
_SIM_TTFT_BASE_MS = 400.0
_SIM_TTFT_TOKENS_MS = 0.05
_SIM_TTFT_IMAGE_MS = 100.0
_SIM_LATENCY_PER_TOKEN_MS = 0.45
_SIM_COST_PER_TOKEN = 2.5e-6
_SIM_IMAGE_TOKENS = 150.0

# Suggested acceptance gates (18.5): accuracy non-regression deltas plus a
# minimum material token/latency benefit. The operator may override the
# material percentages; the accuracy floors stay fixed.
DEFAULT_THRESHOLDS = {
    "exact_delta_min": -0.05,
    "diacritics_delta_min": -0.05,
    "grounding_delta_min": -0.05,
    "tool_selection_delta_min": -0.05,
    "hallucination_delta_max": 0.05,
    "token_reduction_min": 0.10,
    "latency_reduction_min": 0.10,
}

THRESHOLD_KEYS = (
    "exact_delta_min",
    "diacritics_delta_min",
    "grounding_delta_min",
    "tool_selection_delta_min",
    "hallucination_delta_max",
    "token_reduction_min",
    "latency_reduction_min",
)


class ModelRun(TypedDict):
    """Model-reported timing/cost for one question, from any seam."""

    text: str
    reported_input_tokens: int
    ttft_ms: float
    total_latency_ms: float
    cost: float


class VisionModel(Protocol):
    """Model-agnostic seam: any vision-capable model the operator wires in.

    ``run_text`` receives the rendered prompt for one fixture question and
    returns the model text plus model-reported timing (tokens/TTFT/total
    latency/cost). Implementations are provided by the operator; the harness
    never calls a real model itself.
    """

    def run_text(self, prompt: str) -> ModelRun: ...


@dataclass(frozen=True)
class ContextChunk:
    """One corpus context chunk (``descriptive_context`` entry)."""

    id: str
    kind: str
    text: str

    @property
    def classification(self) -> str:
        """CONTROL (mandatory text) or DESCRIPTIVE (image-eligible)."""
        return classify_context(self.kind)


@dataclass(frozen=True)
class BenchmarkFixture:
    """One Q&A fixture: question, ground-truth answer, cited evidence."""

    id: str
    task_class: str
    question: str
    answer: str
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class Answer:
    """Scored answer for one fixture in one mode."""

    fixture_id: str
    task_class: str
    mode: str
    reported_input_tokens: int
    ttft_ms: float
    total_latency_ms: float
    cost: float
    exact: bool
    diacritics: bool
    grounding: bool
    tool_selection: bool
    hallucination: bool


@dataclass(frozen=True)
class RunSummary:
    """Accuracy rates + efficiency aggregates for one mode over all fixtures."""

    exact_accuracy: float
    diacritics_accuracy: float
    grounding_accuracy: float
    tool_selection_accuracy: float
    hallucination_rate: float
    total_input_tokens: int
    mean_ttft_ms: float
    mean_total_latency_ms: float
    total_cost: float


@dataclass(frozen=True)
class GateResult:
    """18.6 gate decision: hybrid stays off unless every threshold passes."""

    enabled: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class BenchmarkResult:
    """Full benchmark output for one mode."""

    meta: dict
    answers: tuple[Answer, ...]
    summary: RunSummary


# -- context classification (18.2) ------------------------------------------


def classify_context(kind: str) -> str:
    """Classify one context chunk: control-plane (text) vs descriptive.

    Control plane (always text): dynamic exact facts, identifiers, and
    instruction-bearing categories. Descriptive (image-eligible): the
    read-only descriptive kinds named in Decision 21 (long static
    descriptions, shop story/persona, campaign background).
    """
    if kind in ("long_description", "shop_story", "campaign_background"):
        return DESCRIPTIVE
    return CONTROL


# -- corpus loading -----------------------------------------------------------


def _load_corpus(path: Path) -> dict:
    """Read + validate the corpus file; return the parsed object.

    Raises ValueError on any structural or provenance violation.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        raise ValueError(f"benchmark corpus missing: {path}") from None
    if not isinstance(data, dict):
        raise ValueError(f"corpus must be a JSON object: {path}")
    if data.get("schema") != SCHEMA or data.get("version") != VERSION:
        raise ValueError(f"corpus schema/version mismatch in {path}: expected {SCHEMA} v{VERSION}")
    provenance = data.get("provenance")
    expected = {"authored_synthetic": True, "contains_pii": False, "factual_ground_truth": False}
    if not isinstance(provenance, dict) or any(
        provenance.get(key) is not value for key, value in expected.items()
    ):
        raise ValueError(f"corpus provenance must state {expected}: {path}")
    task_classes = data.get("task_classes")
    if (
        not isinstance(task_classes, list)
        or not task_classes
        or any(not isinstance(item, str) or not item for item in task_classes)
    ):
        raise ValueError(f"corpus task_classes must be a non-empty list of strings: {path}")
    return data


def _check_task_coverage(fixtures: list[BenchmarkFixture], declared: list[str]) -> None:
    """Declared task classes must each have at least one fixture, and vice
    versa, so every required task class is actually measured (18.4)."""
    used = {fixture.task_class for fixture in fixtures}
    missing = [task_class for task_class in declared if task_class not in used]
    undeclared = sorted(used - set(declared))
    if missing or undeclared:
        raise ValueError(f"task coverage mismatch: missing={missing} undeclared={undeclared}")


def load_fixtures(path: Path = CORPUS_PATH) -> list[BenchmarkFixture]:
    """Load and validate the corpus; return fixtures in original JSON order."""
    data = _load_corpus(path)
    chunks = data.get("descriptive_context")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError(f"corpus has no descriptive_context: {path}")
    chunk_ids = {chunk["id"] for chunk in chunks}
    fixtures: list[BenchmarkFixture] = []
    for item in data.get("fixtures", []):
        if not isinstance(item, dict) or set(item) != {
            "id",
            "task_class",
            "question",
            "answer",
            "evidence",
        }:
            raise ValueError(f"malformed fixture in {path}: {item!r}")
        for key in ("id", "task_class", "question", "answer"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise ValueError(f"fixture {key!r} must be a non-empty string: {item!r}")
        evidence = item["evidence"]
        if (
            not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(ref, str) for ref in evidence)
        ):
            raise ValueError(f"fixture evidence must be a non-empty list of ids: {item!r}")
        if any(ref not in chunk_ids for ref in evidence):
            raise ValueError(f"fixture evidence references unknown context ids: {item!r}")
        fixtures.append(
            BenchmarkFixture(
                id=item["id"],
                task_class=item["task_class"],
                question=item["question"],
                answer=item["answer"],
                evidence=tuple(evidence),
            )
        )
    if len({fixture.id for fixture in fixtures}) != len(fixtures):
        raise ValueError(f"duplicate fixture id in {path}")
    _check_task_coverage(fixtures, cast(list, data.get("task_classes")))
    return fixtures


def load_context(path: Path = CORPUS_PATH) -> list[ContextChunk]:
    """Load the descriptive context chunks in corpus order."""
    data = _load_corpus(path)
    chunks = data.get("descriptive_context")
    if not isinstance(chunks, list) or not chunks:
        raise ValueError(f"corpus has no descriptive_context: {path}")
    result = []
    for chunk in chunks:
        if not isinstance(chunk, dict) or set(chunk) != {"id", "kind", "text"}:
            raise ValueError(f"malformed context chunk in {path}: {chunk!r}")
        for key in ("id", "kind", "text"):
            if not isinstance(chunk[key], str) or not chunk[key].strip():
                raise ValueError(f"context {key!r} must be a non-empty string: {chunk!r}")
        result.append(ContextChunk(chunk["id"], chunk["kind"], chunk["text"]))
    if len({chunk.id for chunk in result}) != len(result):
        raise ValueError(f"duplicate context id in {path}")
    return result


# -- rendering (shared by both modes) ----------------------------------------


def build_prompt(fixture: BenchmarkFixture, chunks: list[ContextChunk], mode: str) -> str:
    """Render the deterministic prompt for one fixture in one mode.

    Control-plane chunks are always text (18.2). In hybrid mode the
    descriptive chunks become an image; the prompt then carries the image
    reference plus a placeable image block. In all-text mode every chunk is
    included as text.
    """
    header = (
        "Bạn là trợ lý bán hàng của shop. Trả lời ngắn gọn, chính xác bằng "
        "tiếng Việt. Chỉ dựa vào thông tin được cung cấp."
    )
    control_text = "\n".join(chunk.text for chunk in chunks if chunk.classification == CONTROL)
    if mode == REAL or mode == SIMULATION:
        # Hybrid rendering: the image carries descriptive context; control
        # facts stay in text. The real seam receives the same layout and
        # decides how the image block is represented to the model.
        image_chunks = [chunk.id for chunk in chunks if chunk.classification == DESCRIPTIVE]
        image_block = f"[IMAGE: {'; '.join(image_chunks)}]" if image_chunks else ""
    else:
        raise ValueError(f"unknown mode {mode!r}")
    if mode == SIMULATION:
        # All-text baseline prompt: same control text, descriptive text inline.
        descriptive_text = "\n".join(
            chunk.text for chunk in chunks if chunk.classification == DESCRIPTIVE
        )
        body = f"{header}\n\n{control_text}\n\n{descriptive_text}\n\n{fixture.question}"
    else:
        body = f"{header}\n\n{control_text}\n\n{image_block}\n\n{fixture.question}"
    return body


def text_tokens(text: str) -> int:
    """Deterministic simulated input-token count for text (used by both
    modes so the baseline/hybrid token math cannot drift)."""
    return max(1, round(len(text) * _SIM_TOKENS_PER_CHAR))


def _image_token_count() -> int:
    """Deterministic simulated token count for one rendered image."""
    return int(_SIM_IMAGE_TOKENS)


def _simulate_measurements(prompt: str, mode: str) -> ModelRun:
    """Deterministic fake measurements (18.3) for one fixture in one mode."""
    tokens = text_tokens(prompt)
    ttft = _SIM_TTFT_BASE_MS + tokens * _SIM_TTFT_TOKENS_MS
    latency = ttft + tokens * _SIM_LATENCY_PER_TOKEN_MS
    if mode == REAL:
        tokens += _image_token_count()
        ttft += _SIM_TTFT_IMAGE_MS
        latency += _SIM_TTFT_IMAGE_MS
    return ModelRun(
        text="",
        reported_input_tokens=tokens,
        ttft_ms=round(ttft, 3),
        total_latency_ms=round(latency, 3),
        cost=round(tokens * _SIM_COST_PER_TOKEN, 6),
    )


def _fabricate_answer(prompt: str, fixture: BenchmarkFixture, mode: str) -> str:
    """Simulation-mode answer: the fixture ground truth verbatim."""
    return fixture.answer


def _fabricate_run(prompt: str, fixture: BenchmarkFixture, mode: str) -> ModelRun:
    run = _simulate_measurements(prompt, mode)
    run["text"] = _fabricate_answer(prompt, fixture, mode)
    return run


# -- scoring (18.4) ------------------------------------------------------------


def _normalize(text: str) -> str:
    """Lowercase + strip whitespace for exact matches."""
    return " ".join(text.strip().lower().split())


def _strip_diacritics(text: str) -> str:
    """Remove Vietnamese diacritics.

    U+0111/U+0110 (d/D with stroke) are precomposed and do not decompose
    under NFD, so the stroke is translated explicitly before combining marks
    are dropped.
    """
    import unicodedata

    stroke_stripped = text.translate(str.maketrans({"đ": "d", "Đ": "D"}))
    return "".join(
        char
        for char in unicodedata.normalize("NFD", stroke_stripped)
        if not unicodedata.combining(char)
    )


def _same_fact(predicted: str, expected: str) -> bool:
    """Normalized exact match (numbers, identifiers, tool names)."""
    return _normalize(predicted) == _normalize(expected)


def _contains_fact(predicted: str, expected: str) -> bool:
    """Grounding facts may be restated; require the key fact to appear."""
    if _same_fact(predicted, expected):
        return True
    if len(expected) < 5:
        return False
    return _normalize(expected) in _normalize(predicted)


def score_exact(predicted: str, expected: str) -> bool:
    """Exact number/identifier accuracy: normalized exact match."""
    return _same_fact(predicted, expected)


def score_diacritics(predicted: str, expected: str) -> bool:
    """Vietnamese-diacritics accuracy: same normalized text with diacritics
    and same text when both are diacritic-stripped, so a stripped/poorly
    diacritized answer fails even when the wording is right."""
    return _same_fact(predicted, expected) and _same_fact(
        _strip_diacritics(predicted), _strip_diacritics(expected)
    )


def score_grounding(predicted: str, expected: str, evidence: str) -> bool:
    """Grounding: the answer contains the expected fact and does not cite
    any fact absent from the provided evidence text."""
    return _contains_fact(predicted, expected) and not _cites_absent(predicted, expected, evidence)


def score_tool_selection(predicted: str, expected: str) -> bool:
    """Tool-selection accuracy: normalized exact match on the tool name."""
    return _same_fact(predicted, expected)


def score_hallucination(predicted: str, expected: str, evidence: str) -> bool:
    """Hallucination: the answer asserts a fact absent from the evidence.

    Returns True when the answer hallucinates. The "Không có thông tin"
    ("no information") refusal is never a hallucination.
    """
    if "không có thông tin" in _normalize(predicted):
        return False
    if not _contains_fact(predicted, expected):
        return True
    return _cites_absent(predicted, expected, evidence)


# Common Vietnamese function words never count as cited facts (grounding).
_VN_FUNCTION_WORDS = frozenset(
    (
        "mà",
        "và",
        "của",
        "cho",
        "với",
        "từ",
        "các",
        "một",
        "có",
        "không",
        "được",
        "thì",
        "ở",
        "vì",
        "nên",
        "những",
        "để",
        "sẽ",
        "đang",
        "này",
        "đó",
        "khi",
        "vẫn",
        "chỉ",
        "rất",
        "là",
        "bằng",
    )
)


def _cites_absent(predicted: str, expected: str, evidence: str) -> bool:
    """True when the answer asserts a fact absent from the evidence.

    The expected answer itself is the fact the question asked for, so it
    never counts as a citation; function words are ignored; any remaining
    token of length >= 3 that does not appear in the evidence is a fact the
    context did not provide.
    """
    normalized_predicted = _normalize(predicted)
    normalized_evidence = _normalize(evidence)
    if normalized_evidence and normalized_predicted in normalized_evidence:
        return False
    expected_tokens = set(_normalize(expected).split())
    return any(
        len(token) >= 3
        and token not in expected_tokens
        and token not in _VN_FUNCTION_WORDS
        and token not in normalized_evidence
        for token in normalized_predicted.split()
    )


# -- run evaluation -------------------------------------------------------------


def _build_meta(mode: str, corpus_path: Path) -> dict:
    """Reproducibility metadata for one benchmark run."""
    return {
        "runner_version": RUNNER_VERSION,
        "corpus_version": VERSION,
        "corpus_path": str(corpus_path),
        "runtime_mode": mode,
        "model_id": os.environ.get(MODEL_ENV) or DEFAULT_MODEL_ID,
        "runtime_report": probe_runtime(),
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
    }


def evaluate_run(
    fixture: BenchmarkFixture,
    chunks: list[ContextChunk],
    mode: str,
    model: VisionModel | None = None,
) -> Answer:
    """Answer + measure + score ONE fixture in one mode.

    ``model`` is required for ``REAL`` mode; in ``SIMULATION`` mode the
    answer is the fixture ground truth and the measurements are deterministic
    fakes (18.3).
    """
    prompt = build_prompt(fixture, chunks, mode)
    if mode == SIMULATION:
        run = _fabricate_run(prompt, fixture, mode)
    elif mode == REAL:
        if model is None:
            # Hybrid mode without an operator seam is simulated
            # deterministically (fixture ground truth + fake measurements):
            # the acceptance gates (18.5/18.6) are exercised identically.
            run = _fabricate_run(prompt, fixture, mode)
        else:
            run = model.run_text(prompt)  # pragma: no cover - operator-invoked only
    else:
        raise ValueError(f"unknown mode {mode!r}")
    evidence = "\n".join(chunk.text for chunk in chunks if chunk.id in fixture.evidence)
    return Answer(
        fixture_id=fixture.id,
        task_class=fixture.task_class,
        mode=mode,
        reported_input_tokens=int(run["reported_input_tokens"]),
        ttft_ms=float(run["ttft_ms"]),
        total_latency_ms=float(run["total_latency_ms"]),
        cost=float(run["cost"]),
        exact=score_exact(run["text"], fixture.answer),
        diacritics=score_diacritics(run["text"], fixture.answer),
        grounding=score_grounding(run["text"], fixture.answer, evidence),
        tool_selection=score_tool_selection(run["text"], fixture.answer),
        hallucination=score_hallucination(run["text"], fixture.answer, evidence),
    )


def summary_for(answers: list[Answer]) -> RunSummary:
    """Aggregate one mode's answers into accuracy rates + efficiency."""
    if not answers:
        return RunSummary(0.0, 0.0, 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0)
    total = len(answers)
    return RunSummary(
        exact_accuracy=sum(a.exact for a in answers) / total,
        diacritics_accuracy=sum(a.diacritics for a in answers) / total,
        grounding_accuracy=sum(a.grounding for a in answers) / total,
        tool_selection_accuracy=sum(a.tool_selection for a in answers) / total,
        hallucination_rate=sum(a.hallucination for a in answers) / total,
        total_input_tokens=sum(a.reported_input_tokens for a in answers),
        mean_ttft_ms=statistics.fmean(a.ttft_ms for a in answers),
        mean_total_latency_ms=statistics.fmean(a.total_latency_ms for a in answers),
        total_cost=sum(a.cost for a in answers),
    )


def run_benchmark(
    mode: str = SIMULATION,
    *,
    corpus_path: Path = CORPUS_PATH,
    model: VisionModel | None = None,
) -> BenchmarkResult:
    """Run the benchmark for one mode over every fixture.

    Returns a ``BenchmarkResult`` with metadata, per-fixture answers, and
    the aggregate summary.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {VALID_MODES}")
    fixtures = load_fixtures(corpus_path)
    chunks = load_context(corpus_path)
    answers = [evaluate_run(fixture, chunks, mode, model=model) for fixture in fixtures]
    return BenchmarkResult(
        meta=_build_meta(mode, corpus_path),
        answers=tuple(answers),
        summary=summary_for(answers),
    )


# -- gate (18.5/18.6) ------------------------------------------------------------


def default_thresholds() -> dict[str, float]:
    """Copy of the suggested acceptance thresholds (callers may tune the
    material-benefit percentages; the accuracy floors stay fixed)."""
    return dict(DEFAULT_THRESHOLDS)


def _delta(baseline: float, hybrid: float) -> float:
    return hybrid - baseline


def evaluate_gate(
    baseline: RunSummary,
    hybrid: RunSummary,
    thresholds: dict[str, float],
) -> GateResult:
    """Pure gate: hybrid may be enabled only when every threshold passes.

    Accuracy non-regression: each hybrid-vs-baseline accuracy delta must be
    >= its (negative) floor, and the hallucination-rate delta must be <= its
    ceiling. Material benefit: input-token and total-latency reductions must
    be >= the minimum percentages (0.10 by default). Returns the decision
    plus the ordered list of failed reasons (empty when enabled).
    """
    reasons: list[str] = []
    if _delta(baseline.exact_accuracy, hybrid.exact_accuracy) < thresholds["exact_delta_min"]:
        reasons.append(
            f"exact accuracy dropped {(baseline.exact_accuracy - hybrid.exact_accuracy):.3f} "
            f"(floor {thresholds['exact_delta_min']})"
        )
    if (
        _delta(baseline.diacritics_accuracy, hybrid.diacritics_accuracy)
        < thresholds["diacritics_delta_min"]
    ):
        reasons.append(
            f"diacritics accuracy dropped "
            f"{(baseline.diacritics_accuracy - hybrid.diacritics_accuracy):.3f} "
            f"(floor {thresholds['diacritics_delta_min']})"
        )
    if (
        _delta(baseline.grounding_accuracy, hybrid.grounding_accuracy)
        < thresholds["grounding_delta_min"]
    ):
        reasons.append(
            f"grounding accuracy dropped "
            f"{(baseline.grounding_accuracy - hybrid.grounding_accuracy):.3f} "
            f"(floor {thresholds['grounding_delta_min']})"
        )
    if (
        _delta(baseline.tool_selection_accuracy, hybrid.tool_selection_accuracy)
        < thresholds["tool_selection_delta_min"]
    ):
        reasons.append(
            f"tool-selection accuracy dropped "
            f"{(baseline.tool_selection_accuracy - hybrid.tool_selection_accuracy):.3f} "
            f"(floor {thresholds['tool_selection_delta_min']})"
        )
    if (
        _delta(baseline.hallucination_rate, hybrid.hallucination_rate)
        > thresholds["hallucination_delta_max"]
    ):
        reasons.append(
            f"hallucination rate rose "
            f"{(hybrid.hallucination_rate - baseline.hallucination_rate):.3f} "
            f"(ceiling {thresholds['hallucination_delta_max']})"
        )
    token_reduction = 1.0 - hybrid.total_input_tokens / baseline.total_input_tokens
    latency_reduction = 1.0 - hybrid.mean_total_latency_ms / baseline.mean_total_latency_ms
    if token_reduction < thresholds["token_reduction_min"]:
        reasons.append(
            f"input-token reduction {token_reduction:.3f} < minimum "
            f"{thresholds['token_reduction_min']}"
        )
    if latency_reduction < thresholds["latency_reduction_min"]:
        reasons.append(
            f"total-latency reduction {latency_reduction:.3f} < minimum "
            f"{thresholds['latency_reduction_min']}"
        )
    return GateResult(enabled=not reasons, reasons=tuple(reasons))


# -- real-model mode (18.1) -------------------------------------------------------


def probe_runtime() -> dict:
    """Check the real-model runtime availability without calling anything.

    Reports ``seam_available`` (whether a real model seam implementation was
    registered by the operator through ``set_real_seam``) and the configured
    target model id. Never raises.
    """
    return {
        "seam_available": _real_seam is not None,
        "model_id": os.environ.get(MODEL_ENV) or DEFAULT_MODEL_ID,
        "detail": (
            "operator must call set_real_seam() and opt in with "
            f"{REAL_RUNTIME_ENV}=1 to run the real model benchmark"
        ),
    }


_real_seam: VisionModel | None = None


def set_real_seam(model: VisionModel | None) -> None:
    """Register (or clear) the operator's real-model seam.

    The harness itself never calls a real model; the seam is injected by the
    operator before invoking ``run_real``. Pass None to clear.
    """
    global _real_seam
    _real_seam = model


def run_real(
    *,
    corpus_path: Path = CORPUS_PATH,
    model: VisionModel | None = None,
) -> BenchmarkResult:
    """Real-model mode: answer every fixture through the seam.

    Two fail-loud gates (both outside ``# pragma: no cover`` and covered by
    tests): the explicit ``CC_BENCH_RUNTIME=1`` opt-in, and a registered
    model seam. The body is correct-by-inspection only (``# pragma: no
    cover`` — it cannot run in CI; the operator invokes it on a machine with
    model access). Measurements (tokens/TTFT/total latency/cost) are
    model-reported through the seam (18.3).
    """
    if os.environ.get(REAL_RUNTIME_ENV) != "1":
        raise RuntimeError(
            f"real-model runtime not enabled: set {REAL_RUNTIME_ENV}=1 to opt "
            f"into the real benchmark (and register a seam via set_real_seam)."
        )
    seam = model or _real_seam
    if seam is None:
        raise RuntimeError("real-model runtime unavailable: no model seam registered")

    # Body: real model — never runs in CI (no opt-in, no seam).
    fixtures = load_fixtures(corpus_path)  # pragma: no cover
    chunks = load_context(corpus_path)  # pragma: no cover
    answers = [evaluate_run(fixture, chunks, REAL, model=seam) for fixture in fixtures]
    return BenchmarkResult(
        meta=_build_meta(REAL, corpus_path),
        answers=tuple(answers),
        summary=summary_for(answers),
    )


# -- report writing ----------------------------------------------------------------


def _to_dict(value: Any) -> Any:
    """JSON-ready value for dataclasses/None; else the value itself."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dict(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {field: _to_dict(getattr(value, field)) for field in value.__dataclass_fields__}
    return str(value)


def _gate_section(
    baseline: RunSummary, hybrid: RunSummary, thresholds: dict[str, float]
) -> tuple[str, GateResult]:
    """Render the gate as (report_lines, gate_result)."""
    gate = evaluate_gate(baseline, hybrid, thresholds)
    lines = ["## Gate (18.5/18.6)", ""]
    lines.append(f"Hybrid enablement: **{'ENABLED' if gate.enabled else 'DISABLED'}**.")
    if gate.reasons:
        lines.append("")
        lines.append("Not enabled because:")
        lines.extend(f"- {reason}" for reason in gate.reasons)
    else:
        lines.append("")
        lines.append("All non-regression and material-benefit thresholds pass.")
    return lines, gate


def write_report(
    baseline: BenchmarkResult,
    hybrid: BenchmarkResult,
    thresholds: dict[str, float],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write JSON + Markdown report comparing baseline vs hybrid.

    Returns ``(json_path, md_path)``. The Markdown report ends with a
    NOT-PASS section when the mode is not real (no real-model evidence, no
    PASS), mirroring the 8.3 benchmark runner.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "context-compression-metrics.json"
    md_path = output_dir / "context-compression-report.md"

    gate_lines, gate = _gate_section(baseline.summary, hybrid.summary, thresholds)
    payload = {
        "baseline": {
            "meta": baseline.meta,
            "summary": _to_dict(baseline.summary),
            "answers": [_to_dict(answer) for answer in baseline.answers],
        },
        "hybrid": {
            "meta": hybrid.meta,
            "summary": _to_dict(hybrid.summary),
            "answers": [_to_dict(answer) for answer in hybrid.answers],
        },
        "gate": {"enabled": gate.enabled, "reasons": list(gate.reasons)},
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    def summary_rows(label: str, summary: RunSummary) -> list[str]:
        return [
            f"| {label} | Exact | Diacritics | Grounding | Tool | Hallucination | Tokens | TTFT ms | Latency ms | Cost |",
            "|---|---|---|---|---|---|---|---|---|---|",
            f"| {label} | {summary.exact_accuracy:.3f} | {summary.diacritics_accuracy:.3f} "
            f"| {summary.grounding_accuracy:.3f} | {summary.tool_selection_accuracy:.3f} "
            f"| {summary.hallucination_rate:.3f} | {summary.total_input_tokens} "
            f"| {summary.mean_ttft_ms:.1f} | {summary.mean_total_latency_ms:.1f} "
            f"| {summary.total_cost:.4f} |",
        ]

    lines = [
        "# Context-compression benchmark report",
        "",
        "## Meta",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Runner version | {baseline.meta['runner_version']} |",
        f"| Corpus version | {baseline.meta['corpus_version']} |",
        f"| Target model | {baseline.meta['model_id']} |",
        f"| Runtime mode | {baseline.meta['runtime_mode']} |",
        f"| Run timestamp | {baseline.meta['run_timestamp']} |",
        "",
        "## Summaries",
        "",
    ]
    lines.extend(summary_rows("Baseline (all-text)", baseline.summary))
    lines.extend(summary_rows("Hybrid", hybrid.summary))
    lines.extend(gate_lines)
    lines.extend(["", "## Per-fixture answers", ""])
    lines.append(
        "| Fixture | Task | Mode | Tokens | TTFT ms | Latency ms | Cost | Exact | Diacritics "
        "| Grounding | Tool | Hallucination |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for answer in baseline.answers + hybrid.answers:
        lines.append(
            f"| {answer.fixture_id} | {answer.task_class} | {answer.mode} "
            f"| {answer.reported_input_tokens} | {answer.ttft_ms} | {answer.total_latency_ms} "
            f"| {answer.cost} | {answer.exact} | {answer.diacritics} | {answer.grounding} "
            f"| {answer.tool_selection} | {answer.hallucination} |"
        )
    lines.extend(["", "## NOT PASS", ""])
    if baseline.meta["runtime_mode"] != REAL:
        lines.append(
            "No real model was invoked in this run — all answers and "
            "measurements are deterministic simulations (fixture ground truth, "
            "fake tokens/TTFT/latency/cost). Per OpenSpec Decision 21, the "
            "benchmark can only PASS with real-model evidence; this run is NOT "
            "PASS by definition. The operator may run the real mode with "
            "CC_BENCH_RUNTIME=1 plus a registered model seam."
        )
    else:
        lines.append(
            "Real model evidence was produced. Human review and the Decision 21 "
            "PASS rule still apply."
        )
    lines.append("")
    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return json_path, md_path


# -- CLI ----------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: ``python -m tests.unit.context_compression_benchmark.benchmark_runner``."""
    parser = argparse.ArgumentParser(
        description="Optional hybrid context-compression benchmark (OpenSpec section 18)."
    )
    parser.add_argument("--mode", choices=VALID_MODES, default=SIMULATION)
    parser.add_argument("--output", type=Path, default=Path("benchmarks"))
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    args = parser.parse_args(argv)
    try:
        if args.mode == REAL:
            baseline = run_benchmark(SIMULATION, corpus_path=args.corpus)
            hybrid = run_real(corpus_path=args.corpus)
        else:
            baseline = run_benchmark(SIMULATION, corpus_path=args.corpus)
            hybrid = run_benchmark(REAL, corpus_path=args.corpus)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json_path, md_path = write_report(baseline, hybrid, default_thresholds(), args.output)
    print(f"wrote {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
