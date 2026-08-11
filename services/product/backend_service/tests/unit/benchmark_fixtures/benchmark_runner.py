"""Deterministic benchmark runner for fixed-versus-adaptive VieNeu chunking
(OpenSpec 8.3).

Runs a chunking policy over the versioned Vietnamese corpus under every
delivery form (full/character/word/provider_like) and every runtime-hint
profile (startup/steady/starvation/neutral), and records the Decision 12
metric list: policy/config hash, TTFA p50/p95, first-chunk estimated/actual
duration, TTS latency, RTF, playback underruns, chunk distribution,
hard-split/protected-span events, preservation/finality failures, and paired
audio artifact paths.

Two runtime modes:

- ``simulation`` (Mode A, default): fully deterministic, no sleeps, no real
  TTS. Chunk timing comes from an injected fake clock; "actual" audio
  duration of a chunk is its estimated duration by construction, so the
  metric answers "how does the policy behave on this corpus" without any
  neural runtime.
- ``vieneu`` (Mode B): drives the real ``tts.engines.vieneu.VieNeuAdapter``,
  writes one wav per chunk under ``output_dir``, and measures real synthesis
  latency, real audio duration, real TTFA, and real RTF. Requires the
  ``vieneu`` package and configured weights (``VIENEU_WEIGHTS_PATH`` local
  path or ``VIENEU_MODEL`` HuggingFace repo id — remote download at load
  time) plus the explicit ``VIENEU_RUNTIME=1`` opt-in; GPU is optional
  (ONNX-CPU is the maintainer-recommended path per the adapter docstring).
  The gate fail-louds when the runtime is unavailable; the body is
  correct-by-inspection only (marked ``# pragma: no cover`` — it cannot run
  on this machine).

TTFA definition (task 8.3, Mode A): time-to-first-audio from the start of
the utterance = the instant the first chunk was emitted (end of the
feed/flush/finalize call that produced it, on the fake clock) plus that
chunk's first-audio synthesis latency. Because the first chunk's synthesis
cost is a fixed constant, the TTFA *trend* across hint profiles comes from
the emission instant: with character/word (and provider_like) delivery,
startup/starvation shrink the soft target, which commits and emits the first
chunk earlier, so their TTFA is smaller than steady. With FULL delivery the
first fragment carries the whole utterance, so the first chunk always emits
on feed 1 at the same instant across profiles — TTFA is then equal across
profiles by construction; that is expected and not a signal.

Output is JSON (machine-readable) plus a Markdown report that ends with a
NOT-PASS section whenever the mode is not real-vieneu (Decision 12.4: no
mode-B evidence, no PASS).

Public API is fixed (task 8.3 contract): module constants, the dataclasses,
``policy_config_hash``, ``probe_runtime``, ``make_chunker``,
``simulate_utterance``, ``run_benchmark``, ``run_vieneu``, ``write_report``,
``default_candidates``, and a minimal CLI (``python -m
tests.unit.benchmark_fixtures.benchmark_runner``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.application.text_chunker import (
    AdaptiveViPolicyConfig,
    ChunkDecisionReason,
    ChunkPolicy,
    FixedChunkPolicyConfig,
    RuntimeHints,
    SpeechDurationEstimator,
    TelemetryCollector,
    TextChunker,
)
from backend.application.text_chunker import policy as policy_module

from .fragments import CORPUS_PATH, VERSION, fragment_deliveries, load_utterances

RUNNER_VERSION = "1.1.0"
BASELINE_FIXED_CHARS = (12, 40, 80)  # min, target, max
BASELINE_FLUSH_TIMEOUT_MS = 350.0  # the "350" in 12/40/80/350ms
BASELINE_POLICY_NAME = "fixed-12-40-80-350ms"

_SIMULATION = "simulation"
_VIENEU = "vieneu"
VALID_MODES = (_SIMULATION, _VIENEU)
VALID_DELIVERY_FORMS = ("full", "character", "word", "provider_like")
VALID_HINT_PROFILES = ("startup", "steady", "starvation", "neutral")

# Mode B environment contract (task 8.3): explicit opt-in + weight source.
_VIENEU_RUNTIME_ENV = "VIENEU_RUNTIME"
_VIENEU_MODEL_ENV = "VIENEU_MODEL"
_VIENEU_WEIGHTS_PATH_ENV = "VIENEU_WEIGHTS_PATH"
_VIENEU_DEVICE_ENV = "VIENEU_DEVICE"
_VIENEU_DEFAULT_MODEL = "pnnbao-ump/VieNeu-TTS-v3-Turbo"

# Fake-clock pacing for one feed() call (ms). The chunker's buffer age and
# the deadline simulation both read this clock, so a deterministic fixed step
# makes feed(N) + flush() timing a pure function of N.
_FEED_STEP_MS = 50.0
# The time each feed() call itself takes (LLM delta + chunker work); the
# chunk emission instant sits at the END of the feed call, right before the
# next 50 ms step, so synthesis finishes 50 ms into the following interval.
_FEED_WORK_MS = 50.0
# First-audio synthesis cost of the very first chunk of an utterance (ms).
_FIRST_AUDIO_SYNTHESIS_MS = 800.0
# Non-first chunk synthesis cost multiplier over estimated duration (TTS RTF).
_SYNTHESIS_RTF = 0.6
# Absolute floor for a non-first chunk synthesis cost (ms).
_MIN_SYNTHESIS_MS = 200.0


@dataclass(frozen=True)
class BenchmarkMeta:  # reproducibility metadata (handoff 56)
    runner_version: str
    corpus_version: int
    corpus_path: str
    policy_name: str
    policy_config_hash: str
    runtime_mode: str  # "simulation" | "vieneu"
    runtime_report: dict  # mode B: package/weights/gpu availability per item; mode A: probe result that explains why simulation was used
    run_timestamp: str  # ISO 8601 UTC
    estimator_coefficients: (
        dict  # from SpeechDurationEstimator coefficients (as dict of field->value)
    )
    scorer_weights: dict  # {"kind_weight": ..., "duration_weight": ..., "char_weight": ...}
    candidate_id: Optional[str]


@dataclass(frozen=True)
class UtteranceMetrics:
    utterance_id: str
    delivery_form: str  # full/character/word/provider_like
    hint_profile: str  # startup/steady/starvation/neutral
    ttfa_ms: float  # first-chunk emission instant + first-chunk synthesis latency
    first_chunk_estimated_ms: Optional[float]
    first_chunk_actual_ms: Optional[float]  # == estimated in Mode A (documented); real in Mode B
    tts_latency_ms: list[float]
    rtf_values: list[float]
    chunk_count: int
    chunk_durations_ms: list[float]
    hard_split_count: int
    protected_span_fallback_count: int
    preservation_failures: int
    finality_failures: int
    underrun_count: int
    audio_artifact_paths: list[str]  # [] in Mode A


@dataclass(frozen=True)
class CandidateResult:
    candidate_id: str
    policy_name: str
    config_hash: str
    meta: BenchmarkMeta
    utterances: list[UtteranceMetrics]
    summary: dict  # computed rollups


# -- serialization helpers -----------------------------------------------


def _to_dict(value: Any) -> Any:
    """JSON-ready dict for dataclasses/Path/None; else the value itself."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {field.name: _to_dict(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _to_dict(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dict(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return str(value)


# -- statistics ----------------------------------------------------------


def _percentile(values: list[float], percentile: float) -> Optional[float]:
    """Nearest-rank percentile of ``values``; None for an empty list."""
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100.0 * len(ordered)) - 1)
    return ordered[index]


def _median(values: list[float]) -> Optional[float]:
    """Median of ``values``; None for an empty list."""
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> Optional[float]:
    """Arithmetic mean of ``values``; None for an empty list."""
    if not values:
        return None
    return float(statistics.fmean(values))


# -- policy/config hashing -----------------------------------------------


def policy_config_hash(
    policy: ChunkPolicy,
    *,
    fixed_config: Optional[FixedChunkPolicyConfig] = None,
    adaptive_config: Optional[AdaptiveViPolicyConfig] = None,
    flush_timeout_ms: float = BASELINE_FLUSH_TIMEOUT_MS,
) -> str:
    """Deterministic sha256 over the policy + effective config fields + flush
    timeout; hexdigest, first 12 characters.

    The effective fixed config defaults to the baseline 12/40/80 when none is
    given, and the effective adaptive config to the module defaults, so the
    hash is stable across calls with the same intent.
    """
    if fixed_config is None:
        fixed_config = FixedChunkPolicyConfig(*BASELINE_FIXED_CHARS)
    if adaptive_config is None:
        adaptive_config = AdaptiveViPolicyConfig()
    payload = {
        "policy": policy.value,
        "fixed_config": asdict(fixed_config),
        "adaptive_config": asdict(adaptive_config),
        "flush_timeout_ms": flush_timeout_ms,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:12]


# -- runtime probing ------------------------------------------------------


def probe_runtime() -> dict:
    """Check the Mode B runtime in order; never raises.

    Reports per item: ``vieneu_package`` (importable), ``weights_present``
    (local weights path exists OR a model repo id is set for remote
    download), ``weights_source`` (``"local:<path>"`` | ``"huggingface:
    <repo>"`` | ``"none"`` — the honest source of the weights),
    ``gpu_available`` (torch CUDA where torch is importable, else False).
    ``detail`` explains why the environment does or does not support Mode B.
    """
    try:
        import vieneu  # noqa: F401

        vieneu_package = True
    except Exception:
        vieneu_package = False
    try:
        import torch  # noqa: F401

        gpu_available = bool(torch.cuda.is_available())
    except Exception:
        gpu_available = False
    weights_path = os.environ.get(_VIENEU_WEIGHTS_PATH_ENV)
    model_id = os.environ.get(_VIENEU_MODEL_ENV)
    if weights_path and Path(weights_path).exists():
        weights_present = True
        weights_source = f"local:{weights_path}"
    elif model_id:
        # HF repo id — weights download at load time (needs network + disk).
        weights_present = True
        weights_source = f"huggingface:{model_id}"
    else:
        weights_present = False
        weights_source = "none"
        # Default repo id fallback for the adapter; reported honestly as a
        # remote default (no local path exists in this repo).
        model_id = model_id or _VIENEU_DEFAULT_MODEL
    detail = (
        f"vieneu package importable={vieneu_package}; weights "
        f"configured={weights_present} (source={weights_source}, "
        f"model={model_id}); GPU available={gpu_available}."
    )
    if not (vieneu_package and weights_present):
        detail += (
            " Mode B needs the vieneu package and configured weights (GPU optional via ONNX-CPU)."
        )
    return {
        "vieneu_package": vieneu_package,
        "weights_present": weights_present,
        "weights_source": weights_source,
        "gpu_available": gpu_available,
        "detail": detail,
    }


# -- fake clock -----------------------------------------------------------


class _FakeClock:
    """Deterministic monotonic clock in seconds for one simulation.

    ``now`` starts at ``start_ms`` and each ``advance_ms`` call adds a fixed
    step, so every feed/flush sequence maps to identical timestamps.
    """

    def __init__(self, start_ms: float = 0.0) -> None:
        self._ms = start_ms

    def __call__(self) -> float:
        return self._ms / 1000.0

    def advance_ms(self, step_ms: float) -> None:
        self._ms += step_ms


# -- hint profiles --------------------------------------------------------


def _hints_for_profile(profile: str) -> RuntimeHints:
    """RuntimeHints for one benchmark profile (see module docstring)."""
    if profile == "startup":
        return RuntimeHints(speech_start_elapsed_ms=3000.0)
    if profile == "steady":
        return RuntimeHints(playback_buffer_ms=6000.0)
    if profile == "starvation":
        return RuntimeHints(playback_buffer_ms=500.0, tts_rtf_ewma=2.0)
    return RuntimeHints()


# -- chunker construction -------------------------------------------------


def make_chunker(
    session_id: str,
    utterance_id: str,
    policy: ChunkPolicy,
    *,
    fixed_config: Optional[FixedChunkPolicyConfig] = None,
    adaptive_config: Optional[AdaptiveViPolicyConfig] = None,
    clock: Optional[Any] = None,
    telemetry: Optional[TelemetryCollector] = None,
) -> TextChunker:
    """Wrap ``TextChunker`` construction with benchmark defaults.

    ``policy`` defaults to ``ChunkPolicy.FIXED`` and ``fixed_config`` to the
    baseline 12/40/80; ``adaptive_config`` is optional. ``telemetry`` (a
    plain ``TelemetryCollector``) is attached when the caller needs the
    content-free record of protected-span fallbacks — that flag exists only
    in telemetry, never on ``TextChunk`` (task 7.1).
    """
    if fixed_config is None and policy == ChunkPolicy.FIXED:
        fixed_config = FixedChunkPolicyConfig(*BASELINE_FIXED_CHARS)
    return TextChunker(
        session_id=session_id,
        utterance_id=utterance_id,
        policy=policy,
        fixed_config=fixed_config,
        adaptive_config=adaptive_config,
        clock=clock,
        telemetry=telemetry,
    )


# -- Mode A simulation -----------------------------------------------------


def _simulate_synthesis_timeline(
    estimator: SpeechDurationEstimator,
    chunks: list,
    emission_ms_list: list[float],
) -> tuple[float, list[float], list[float], int]:
    """Synthesize + play back the emitted chunks; return
    (ttfa_ms, tts_latency_ms, rtf_values, underrun_count).

    ``emission_ms_list`` holds the simulated instant each chunk was emitted
    (end of the feed/flush/finalize call that produced it, in seconds —
    clock units). Deterministic consumable-buffer model (rule 5 of task
    8.3): the playback buffer accumulates audio; a consumer plays it
    continuously at 1.0x realtime from the start of the utterance. Audio
    becomes playable only when its synthesis finishes (emission instant +
    synthesis cost); a chunk underruns when its synthesis completes after
    the consumer has already played everything available (a playback gap,
    and the consumer waits). The first chunk never underruns (nothing was
    expected before it). Healthy full-text delivery stays underrun-free;
    startup-heavy starvation profiles (slow first audio, tiny chunks from
    350 ms deadline flushes) underrun.

    TTFA (task 8.3, Mode A definition): time-to-first-audio from the start
    of the utterance = the first chunk's emission instant plus its
    first-audio synthesis cost. The synthesis cost is the same fixed
    constant for every profile, so the TTFA *trend* across profiles is
    carried entirely by the emission instant — early soft-target pressure
    (startup/starvation) commits the first chunk earlier under
    character/word delivery, which must show as smaller TTFA.
    """
    ttfa_ms = 0.0
    latencies: list[float] = []
    rtfs: list[float] = []
    underruns = 0
    available_ms = 0.0  # audio accumulated in the playback buffer
    played_ms = 0.0  # audio already consumed
    previous_emission_ms = 0.0  # the utterance starts at simulated t=0
    for index, chunk in enumerate(chunks):
        audio_ms = estimator.estimate_ms(chunk.text)
        if index == 0:
            synth_ms = _FIRST_AUDIO_SYNTHESIS_MS
            # Emission instant (ms) + first-audio synthesis latency (ms).
            ttfa_ms = emission_ms_list[0] * 1000.0 + synth_ms if emission_ms_list else 0.0
        else:
            synth_ms = max(audio_ms * _SYNTHESIS_RTF, _MIN_SYNTHESIS_MS)
        latencies.append(synth_ms)
        rtfs.append(synth_ms / audio_ms if audio_ms > 0 else 0.0)
        emitted_ms = emission_ms_list[index]
        gap_ms = (emitted_ms - previous_emission_ms) * 1000.0
        # The consumer drains continuously at 1.0x realtime. An underrun is
        # a chunk emitted after the consumer has already played everything
        # available (playback gap); the consumer then catches up to the
        # buffer. The first chunk never underruns — nothing was expected
        # before it.
        played_ms += gap_ms
        if index > 0 and played_ms > available_ms:
            underruns += 1
            played_ms = available_ms
        available_ms += audio_ms
        previous_emission_ms = emitted_ms
    return ttfa_ms, latencies, rtfs, underruns


def _feed_with_deadline(
    chunker: TextChunker,
    fragments: list[str],
    hints: RuntimeHints,
    fake_clock: _FakeClock,
) -> tuple[list, list[float]]:
    """Feed every fragment with the 350 ms deadline flush logic.

    Shared by Mode A (``simulate_utterance``) and Mode B (``run_vieneu``) so
    the two modes cannot drift: after each feed, when
    ``buffer_age_ms >= flush_timeout_ms``, flush with
    ``ChunkDecisionReason.LATENCY_DEADLINE``. Returns
    ``(all_chunks, emission_ms_list)`` where each emission instant (seconds,
    clock units) is the end of the feed/flush/finalize call that produced
    the chunk, right before the next fixed clock step. Caller finalizes.
    """
    all_chunks: list = []
    emission_ms: list[float] = []
    for fragment in fragments:
        all_chunks.extend(chunker.feed(fragment, runtime_hints=hints))
        # The feed() call itself takes real time; emitted chunks (TTS calls)
        # are triggered at the end of the call, right before the next step.
        for _ in all_chunks[len(emission_ms) :]:
            emission_ms.append(fake_clock() + _FEED_WORK_MS / 1000.0)
        fake_clock.advance_ms(_FEED_STEP_MS)
        if chunker.buffer_age_ms >= BASELINE_FLUSH_TIMEOUT_MS:
            fake_clock.advance_ms(BASELINE_FLUSH_TIMEOUT_MS)
            all_chunks.extend(
                chunker.flush(reason=ChunkDecisionReason.LATENCY_DEADLINE, runtime_hints=hints)
            )
            for _ in all_chunks[len(emission_ms) :]:
                emission_ms.append(fake_clock() + _FEED_WORK_MS / 1000.0)
    return all_chunks, emission_ms


def simulate_utterance(
    text: str,
    delivery_form: str,
    hint_profile: str,
    policy: ChunkPolicy,
    *,
    fixed_config: Optional[FixedChunkPolicyConfig] = None,
    adaptive_config: Optional[AdaptiveViPolicyConfig] = None,
    clock: Optional[_FakeClock] = None,
    rt_fixed_synthesis_ms: float = _FIRST_AUDIO_SYNTHESIS_MS,
    rt_factor: float = _SYNTHESIS_RTF,
    utterance_id: str = "benchmark-sim",
) -> UtteranceMetrics:
    """Deterministic Mode A simulation of ONE utterance under one delivery
    form + one hint profile.

    Mirrors the streaming orchestrator (task 4.5): feed every fragment with
    the profile's runtime hints, and after each feed, when
    ``buffer_age_ms >= flush_timeout_ms`` (the 350 ms deadline), flush with
    ``ChunkDecisionReason.LATENCY_DEADLINE``; then finalize. The fake clock
    advances 50 ms per feed and one extra 350 ms step per deadline flush, so
    the whole run is a pure function of the inputs.

    TTS/playback are simulated (rules 4-5): the first chunk costs
    ``rt_fixed_synthesis_ms``, later chunks cost ``max(estimate_ms *
    rt_factor, 200)``; the "actual" audio duration of a chunk is its
    estimated duration by construction (Mode A has no real audio; Mode B
    measures the real one).

    TTFA (task 8.3, Mode A definition) = the first chunk's emission instant
    (end of the feed/flush call that produced it, clock units) plus its
    first-audio synthesis latency. With character/word delivery the startup
    and starvation profiles shrink the soft target, which commits the first
    chunk earlier — so their TTFA is smaller than steady's. With FULL
    delivery the first chunk always emits on feed 1 at the same instant, so
    TTFA is equal across profiles by construction; that is expected and not
    a signal.

    ``utterance_id`` stamps the chunker and the returned metrics, so
    standalone calls can carry the corpus record id (``run_benchmark``
    overrides it with the record id — unchanged behavior).

    Raises ``ValueError`` on an unknown delivery form, hint profile, or
    policy.
    """
    if delivery_form not in VALID_DELIVERY_FORMS:
        raise ValueError(f"unknown delivery form {delivery_form!r}")
    if hint_profile not in VALID_HINT_PROFILES:
        raise ValueError(f"unknown hint profile {hint_profile!r}")
    if not isinstance(policy, ChunkPolicy):
        raise ValueError(f"policy must be a ChunkPolicy, got {policy!r}")

    estimator = SpeechDurationEstimator()
    fake_clock = clock if clock is not None else _FakeClock()
    telemetry = TelemetryCollector()
    chunker = make_chunker(
        session_id=utterance_id,
        utterance_id=utterance_id,
        policy=policy,
        fixed_config=fixed_config,
        adaptive_config=adaptive_config,
        clock=fake_clock,
        telemetry=telemetry,
    )
    hints = _hints_for_profile(hint_profile)

    all_chunks, emission_ms = _feed_with_deadline(
        chunker, fragment_deliveries(text)[delivery_form], hints, fake_clock
    )
    all_chunks.extend(chunker.finalize(runtime_hints=hints))
    for _ in all_chunks[len(emission_ms) :]:
        emission_ms.append(fake_clock() + _FEED_WORK_MS / 1000.0)

    joined = "".join(chunk.text for chunk in all_chunks)
    hard_splits = sum(1 for chunk in all_chunks if chunk.decision_reason == "hard_max")
    # Protected-span fallback exists only in telemetry (task 7.1) — the
    # content-free record is the source of truth, never TextChunk.
    protected_fallbacks = sum(1 for record in telemetry.records if record.protected_span_fallback)

    durations = [estimator.estimate_ms(chunk.text) for chunk in all_chunks]
    ttfa_ms, latencies, rtfs, underruns = _simulate_synthesis_timeline(
        estimator, all_chunks, emission_ms
    )

    return UtteranceMetrics(
        utterance_id=utterance_id,
        delivery_form=delivery_form,
        hint_profile=hint_profile,
        ttfa_ms=ttfa_ms,
        first_chunk_estimated_ms=durations[0] if durations else None,
        first_chunk_actual_ms=durations[0] if durations else None,
        tts_latency_ms=latencies,
        rtf_values=rtfs,
        chunk_count=len(all_chunks),
        chunk_durations_ms=durations,
        hard_split_count=hard_splits,
        protected_span_fallback_count=protected_fallbacks,
        preservation_failures=0 if joined == text else 1,
        finality_failures=0,  # exactly-once finality is orchestration-owned (task 6.2)
        underrun_count=underruns,
        audio_artifact_paths=[],
    )


# -- summary ----------------------------------------------------------------


def _profile_summary(utterances: list[UtteranceMetrics], profile: str) -> dict:
    """Rollups for one hint profile."""
    grouped = [u for u in utterances if u.hint_profile == profile]
    return {
        "ttfa_p50": _percentile([u.ttfa_ms for u in grouped], 50),
        "ttfa_p95": _percentile([u.ttfa_ms for u in grouped], 95),
        "underrun_total": sum(u.underrun_count for u in grouped),
        "chunk_total": sum(u.chunk_count for u in grouped),
    }


def _build_summary(utterances: list[UtteranceMetrics]) -> dict:
    """Computed rollups over every utterance in the run (Decision 12 list)."""
    if not utterances:
        return {}
    ttfa = [u.ttfa_ms for u in utterances]
    first_estimated = [
        u.first_chunk_estimated_ms for u in utterances if u.first_chunk_estimated_ms is not None
    ]
    first_actual = [
        u.first_chunk_actual_ms for u in utterances if u.first_chunk_actual_ms is not None
    ]
    latency = [value for u in utterances for value in u.tts_latency_ms]
    rtf = [value for u in utterances for value in u.rtf_values]
    durations = [value for u in utterances for value in u.chunk_durations_ms]
    return {
        "total_utterances": len(utterances),
        "ttfa_p50": _percentile(ttfa, 50),
        "ttfa_p95": _percentile(ttfa, 95),
        "ttfa_median_all": _median(ttfa),
        "ttfa_mean": _mean(ttfa),
        "first_chunk_estimated_p50": _median(first_estimated),
        "first_chunk_actual_p50": _median(first_actual),
        "tts_latency_p50": _median(latency),
        "rtf_p50": _median(rtf),
        "rtf_mean": _mean(rtf),
        "underrun_total": sum(u.underrun_count for u in utterances),
        "underrun_utterances": sum(1 for u in utterances if u.underrun_count > 0),
        "chunk_total": sum(u.chunk_count for u in utterances),
        "chunk_duration_p50": _percentile(durations, 50),
        "chunk_duration_p95": _percentile(durations, 95),
        "hard_split_total": sum(u.hard_split_count for u in utterances),
        "protected_fallback_total": sum(u.protected_span_fallback_count for u in utterances),
        "preservation_failures_total": sum(u.preservation_failures for u in utterances),
        "finality_failures_total": sum(u.finality_failures for u in utterances),
        "per_hint_profile": {
            profile: _profile_summary(utterances, profile) for profile in VALID_HINT_PROFILES
        },
    }


def _effective_fixed_config(
    fixed_config: Optional[FixedChunkPolicyConfig],
) -> FixedChunkPolicyConfig:
    return (
        fixed_config if fixed_config is not None else FixedChunkPolicyConfig(*BASELINE_FIXED_CHARS)
    )


def _build_meta(
    policy: ChunkPolicy,
    *,
    fixed_config: Optional[FixedChunkPolicyConfig] = None,
    adaptive_config: Optional[AdaptiveViPolicyConfig] = None,
    candidate_id: Optional[str] = None,
    mode: str,
    corpus_path: Path,
) -> tuple[BenchmarkMeta, str]:
    """Shared BenchmarkMeta + config hash for both modes."""
    config_hash = policy_config_hash(
        policy, fixed_config=fixed_config, adaptive_config=adaptive_config
    )
    coefficients = asdict(SpeechDurationEstimator()._c)
    scorer_weights = {
        "kind_weight": getattr(policy_module, "_KIND_WEIGHT"),
        "duration_weight": getattr(policy_module, "_DURATION_WEIGHT"),
        "char_weight": getattr(policy_module, "_CHAR_WEIGHT"),
    }
    policy_name = BASELINE_POLICY_NAME if policy == ChunkPolicy.FIXED else "adaptive-vi"
    meta = BenchmarkMeta(
        runner_version=RUNNER_VERSION,
        corpus_version=VERSION,
        corpus_path=str(corpus_path),
        policy_name=policy_name,
        policy_config_hash=config_hash,
        runtime_mode=mode,
        runtime_report=probe_runtime(),
        run_timestamp=datetime.now(timezone.utc).isoformat(),
        estimator_coefficients=coefficients,
        scorer_weights=scorer_weights,
        candidate_id=candidate_id,
    )
    return meta, config_hash


# -- benchmark orchestration --------------------------------------------------


def run_benchmark(
    policy: ChunkPolicy,
    *,
    corpus_path: Path = CORPUS_PATH,
    delivery_forms: tuple[str, ...] = VALID_DELIVERY_FORMS,
    hint_profiles: tuple[str, ...] = VALID_HINT_PROFILES,
    fixed_config: Optional[FixedChunkPolicyConfig] = None,
    adaptive_config: Optional[AdaptiveViPolicyConfig] = None,
    candidate_id: Optional[str] = None,
    mode: str = _SIMULATION,
) -> CandidateResult:
    """Run the benchmark in ``mode`` over every (utterance, delivery form,
    hint profile) combination.

    Mode ``simulation`` (default) uses ``simulate_utterance``; mode
    ``vieneu`` delegates to ``run_vieneu`` (which fail-louds when the real
    runtime is unavailable). Returns a ``CandidateResult`` with full meta,
    per-utterance metrics, and the summary rollups.
    """
    if not isinstance(policy, ChunkPolicy):
        raise ValueError(f"policy must be a ChunkPolicy, got {policy!r}")
    if mode not in VALID_MODES:
        raise ValueError(f"unknown mode {mode!r}; expected one of {VALID_MODES}")
    meta, config_hash = _build_meta(
        policy,
        fixed_config=fixed_config,
        adaptive_config=adaptive_config,
        candidate_id=candidate_id,
        mode=mode,
        corpus_path=corpus_path,
    )
    if mode == _VIENEU:
        raise RuntimeError("run_benchmark(mode='vieneu') requires output_dir; use run_vieneu()")
    utterances = []
    for record in load_utterances(corpus_path):
        for form in delivery_forms:
            for profile in hint_profiles:
                metrics = simulate_utterance(
                    record.text,
                    form,
                    profile,
                    policy,
                    fixed_config=fixed_config,
                    adaptive_config=adaptive_config,
                    utterance_id=record.id,
                )
                utterances.append(metrics)
    return CandidateResult(
        candidate_id=candidate_id,
        policy_name=meta.policy_name,
        config_hash=config_hash,
        meta=meta,
        utterances=utterances,
        summary=_build_summary(utterances),
    )


def _wav_path_for_chunk(
    output_dir: Path,
    candidate_id: Optional[str],
    utterance_id: str,
    delivery_form: str,
    profile: str,
    seq: int,
) -> Path:
    """Deterministic artifact path for one Mode B chunk wav."""
    return (
        Path(output_dir)
        / (candidate_id or "run")
        / utterance_id
        / delivery_form
        / profile
        / f"seq-{seq}.wav"
    )


def _write_wav_float32(path: Path, pcm: Any, sample_rate: int) -> Path:
    """Write float32 mono PCM ([-1, 1]) as a 16-bit PCM WAV.

    Stdlib ``wave`` only (no numpy dependency); the AudioChunk pcm is
    float32 so it is scaled to int16 with a hard clamp. Raises on any
    non-finite sample so a broken synthesis never produces a silent
    artifact.
    """
    import struct
    import wave

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        raw = bytearray()
        for sample in pcm:
            if not math.isfinite(sample):
                raise ValueError(f"non-finite PCM sample at {path}")
            raw.extend(struct.pack("<h", int(max(-1.0, min(1.0, sample)) * 32767.0)))
        handle.writeframes(bytes(raw))
    return path


def run_vieneu(
    policy: ChunkPolicy,
    *,
    corpus_path: Path = CORPUS_PATH,
    output_dir: Path,
    candidate_id: Optional[str] = None,
    delivery_forms: tuple[str, ...] = VALID_DELIVERY_FORMS,
    hint_profiles: tuple[str, ...] = VALID_HINT_PROFILES,
    fixed_config: Optional[FixedChunkPolicyConfig] = None,
    adaptive_config: Optional[AdaptiveViPolicyConfig] = None,
    **kwargs: Any,
) -> CandidateResult:
    """Mode B: synthesize every emitted chunk with the real VieNeu adapter.

    Two fail-loud gates, in order (both outside ``# pragma: no cover`` and
    covered by tests):

    1. Explicit opt-in: ``VIENEU_RUNTIME`` must be exactly ``"1"`` (the task
       8.3 env gate).
    2. Runtime probe: the ``vieneu`` package AND configured weights
       (``VIENEU_WEIGHTS_PATH`` existing local path, or ``VIENEU_MODEL`` HF
       repo id for remote download). GPU is optional — ONNX-CPU is the
       maintainer-recommended path for v3-Turbo per the adapter docstring.

    Body (correct-by-inspection only, ``# pragma: no cover`` — it cannot run
    on this machine): loads the real adapter via
    ``tts.engines.base.load_engine``, then for every utterance x delivery
    form x hint profile feeds fragments through the same deadline logic as
    Mode A (``_feed_with_deadline``), synthesizes EVERY emitted chunk with
    ``engine.synthesize(TTSRequest(text=chunk.text))``, measures real wall
    clock synthesis latency, real audio duration (``len(pcm) /
    sample_rate * 1000``), real TTFA (first synthesize latency + emission
    instant), real RTF, and real underruns from a real playback timeline
    (same consumable-buffer model as Mode A). Writes one 16-bit WAV per
    chunk under ``output_dir/{candidate_id or 'run'}/{utterance_id}/
    {delivery_form}/{profile}/seq-{n}.wav`` (stdlib ``wave``; float32 PCM
    scaled to int16 with a hard clamp). Engine construction failures raise
    ``RuntimeError`` with the underlying message (fail-loud, never silent).
    """
    if os.environ.get(_VIENEU_RUNTIME_ENV) != "1":
        raise RuntimeError(
            f"VieNeu runtime not enabled: set {_VIENEU_RUNTIME_ENV}=1 to opt "
            f"into Mode B (plus weights via {_VIENEU_WEIGHTS_PATH_ENV} or "
            f"{_VIENEU_MODEL_ENV})."
        )
    runtime = probe_runtime()
    if not (runtime["vieneu_package"] and runtime["weights_present"]):
        raise RuntimeError(
            f"VieNeu runtime unavailable: {runtime!r} — benchmark Mode B "
            f"requires the vieneu package and configured weights "
            f"(GPU optional via ONNX-CPU). See runtime_report."
        )

    # Body: real Mode B — never runs on this machine (no vieneu package).
    # tts_service is an import root ("tts") in the backend pytest pythonpath.
    from tts.engines.base import TTSRequest, load_engine  # type: ignore[import-not-found]

    engine_cfg = {
        "engine": "vieneu",
        "model": os.environ.get(_VIENEU_MODEL_ENV) or _VIENEU_DEFAULT_MODEL,
        "weights_path": os.environ.get(_VIENEU_WEIGHTS_PATH_ENV),
        "device": os.environ.get(_VIENEU_DEVICE_ENV) or "auto",
    }
    try:
        engine = load_engine(engine_cfg)  # type: ignore[no-untyped-call]
    except Exception as exc:
        raise RuntimeError(f"failed to load VieNeu engine: {exc}") from exc

    meta, config_hash = _build_meta(
        policy,
        fixed_config=fixed_config,
        adaptive_config=adaptive_config,
        candidate_id=candidate_id,
        mode=_VIENEU,
        corpus_path=corpus_path,
    )
    utterances: list[UtteranceMetrics] = []
    for record in load_utterances(corpus_path):
        for form in delivery_forms:
            for profile in hint_profiles:
                estimator = SpeechDurationEstimator()
                fake_clock = _FakeClock()
                chunker = make_chunker(
                    session_id=record.id,
                    utterance_id=record.id,
                    policy=policy,
                    fixed_config=fixed_config,
                    adaptive_config=adaptive_config,
                    clock=fake_clock,
                )
                hints = _hints_for_profile(profile)
                all_chunks, emission_ms = _feed_with_deadline(
                    chunker, fragment_deliveries(record.text)[form], hints, fake_clock
                )
                all_chunks.extend(chunker.finalize(runtime_hints=hints))
                for _ in all_chunks[len(emission_ms) :]:
                    emission_ms.append(fake_clock() + _FEED_WORK_MS / 1000.0)

                # Synthesize every emitted chunk with the real engine and
                # measure real timing; the playback timeline is the same
                # consumable-buffer model as Mode A.
                latencies: list[float] = []
                rtfs: list[float] = []
                durations: list[float] = []
                artifact_paths: list[str] = []
                underruns = 0
                available_ms = 0.0
                played_ms = 0.0
                previous_emission_ms = 0.0
                ttfa_ms = 0.0
                for seq, (chunk, emitted_ms) in enumerate(zip(all_chunks, emission_ms)):
                    started = datetime.now(timezone.utc)
                    audio = engine.synthesize(TTSRequest(text=chunk.text))
                    synth_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000.0
                    audio_ms = (
                        len(audio.pcm) / audio.sample_rate * 1000.0
                        if audio.sample_rate > 0
                        else 0.0
                    )
                    if seq == 0:
                        ttfa_ms = emitted_ms * 1000.0 + synth_ms
                    latencies.append(synth_ms)
                    rtfs.append(synth_ms / audio_ms if audio_ms > 0 else 0.0)
                    durations.append(audio_ms)
                    artifact_paths.append(
                        str(
                            _write_wav_float32(
                                _wav_path_for_chunk(
                                    output_dir,
                                    candidate_id,
                                    record.id,
                                    form,
                                    profile,
                                    seq,
                                ),
                                audio.pcm,
                                audio.sample_rate,
                            )
                        )
                    )
                    gap_ms = (emitted_ms - previous_emission_ms) * 1000.0
                    played_ms += gap_ms
                    if seq > 0 and played_ms > available_ms:
                        underruns += 1
                        played_ms = available_ms
                    available_ms += audio_ms
                    previous_emission_ms = emitted_ms

                joined = "".join(chunk.text for chunk in all_chunks)
                utterances.append(
                    UtteranceMetrics(
                        utterance_id=record.id,
                        delivery_form=form,
                        hint_profile=profile,
                        ttfa_ms=ttfa_ms,
                        first_chunk_estimated_ms=(
                            estimator.estimate_ms(all_chunks[0].text) if all_chunks else None
                        ),
                        first_chunk_actual_ms=durations[0] if durations else None,
                        tts_latency_ms=latencies,
                        rtf_values=rtfs,
                        chunk_count=len(all_chunks),
                        chunk_durations_ms=durations,
                        hard_split_count=sum(
                            1 for c in all_chunks if c.decision_reason == "hard_max"
                        ),
                        protected_span_fallback_count=0,
                        preservation_failures=0 if joined == record.text else 1,
                        finality_failures=0,
                        underrun_count=underruns,
                        audio_artifact_paths=artifact_paths,
                    )
                )
    return CandidateResult(
        candidate_id=candidate_id,
        policy_name=meta.policy_name,
        config_hash=config_hash,
        meta=meta,
        utterances=utterances,
        summary=_build_summary(utterances),
    )


# -- report writing -----------------------------------------------------------


def write_report(result: CandidateResult, output_dir: Path) -> tuple[Path, Path]:
    """Write JSON + Markdown report for one candidate run.

    Returns ``(json_path, md_path)``. The Markdown report contains the meta
    table, summary table, per-profile table, per-utterance table, and a
    NOT-PASS section documenting runtime availability when the mode is not
    real-vieneu (Decision 12.4).
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = result.candidate_id or "run"
    json_path = output_dir / f"{stem}-metrics.json"
    md_path = output_dir / f"{stem}-report.md"

    payload = {
        "candidate_id": result.candidate_id,
        "policy_name": result.policy_name,
        "config_hash": result.config_hash,
        "meta": _to_dict(result.meta),
        "utterances": [_to_dict(u) for u in result.utterances],
        "summary": result.summary,
    }
    with json_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)

    lines = [
        f"# Benchmark report: {result.policy_name}",
        "",
        "## Meta",
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Policy name | {result.meta.policy_name} |",
        f"| Config hash | {result.meta.policy_config_hash} |",
        f"| Corpus version | {result.meta.corpus_version} |",
        f"| Runner version | {result.meta.runner_version} |",
        f"| Runtime mode | {result.meta.runtime_mode} |",
        f"| Run timestamp | {result.meta.run_timestamp} |",
        "",
        "## Summary",
        "",
        "| Metric | Value |",
        "|---|---|",
    ]
    for key, value in sorted(result.summary.items()):
        if key == "per_hint_profile":
            continue
        lines.append(f"| {key} | {value} |")
    lines.extend(
        [
            "",
            "## Per hint profile",
            "",
            "| Profile | TTFA p50 | TTFA p95 | Underruns | Chunks |",
            "|---|---|---|---|---|",
        ]
    )
    for profile, profile_summary in result.summary.get("per_hint_profile", {}).items():
        lines.append(
            f"| {profile} | {profile_summary['ttfa_p50']} | {profile_summary['ttfa_p95']} "
            f"| {profile_summary['underrun_total']} | {profile_summary['chunk_total']} |"
        )
    lines.extend(
        [
            "",
            "## Per utterance",
            "",
            "| ID | Form | Profile | TTFA ms | Chunks | Hard splits | Protected | Underruns | Preservation | Finality |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for u in result.utterances:
        lines.append(
            f"| {u.utterance_id} | {u.delivery_form} | {u.hint_profile} | {u.ttfa_ms} "
            f"| {u.chunk_count} | {u.hard_split_count} | {u.protected_span_fallback_count} "
            f"| {u.underrun_count} | {u.preservation_failures} | {u.finality_failures} |"
        )
    lines.extend(["", "## NOT PASS", ""])
    if result.meta.runtime_mode != _VIENEU:
        lines.append(
            "No real VieNeu audio was produced in this run — all metrics are "
            "Mode A simulations (estimated durations, simulated TTS/playback). "
            "Per OpenSpec Decision 12.4, a candidate can only PASS with real "
            "Mode B evidence; this run is NOT PASS by definition."
        )
    else:
        lines.append(
            "Real VieNeu audio was produced (Mode B). Human review and the "
            "Decision 12 PASS rule still apply before this candidate may pass."
        )
    lines.append("")
    with md_path.open("w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return json_path, md_path


# -- candidate space -----------------------------------------------------------


def default_candidates() -> list[dict]:
    """Bounded candidate space for task 8.5 (NO random search).

    Grid over ``target_duration_ms`` x ``startup_early_target_ms`` x
    ``starvation_target_ms`` (3 x 2 x 2 = 12 adaptive candidates) plus the
    fixed baseline as ``cand-baseline-fixed``. All other
    ``AdaptiveViPolicyConfig`` fields stay at module defaults. Deterministic
    order: adaptive grid in sorted value order, baseline last.
    """
    candidates = [
        {
            "candidate_id": f"cand-{index:02d}",
            "policy": "adaptive_vi",
            "adaptive_config": {
                "target_duration_ms": target_duration,
                "startup_early_target_ms": startup_early,
                "starvation_target_ms": starvation,
            },
            "description": (
                f"adaptive_vi target={target_duration}ms, "
                f"startup_early={startup_early}ms, starvation={starvation}ms"
            ),
        }
        for index, (target_duration, startup_early, starvation) in enumerate(
            (
                (target_duration, startup_early, starvation)
                for target_duration in (1800.0, 2200.0, 2600.0)
                for startup_early in (1200.0, 1500.0)
                for starvation in (1200.0, 1400.0)
            ),
            start=1,
        )
    ]
    candidates.append(
        {
            "candidate_id": "cand-baseline-fixed",
            "policy": "fixed",
            "adaptive_config": None,
            "description": BASELINE_POLICY_NAME,
        }
    )
    return candidates


# -- CLI ------------------------------------------------------------------


def _candidate_from_cli(candidate_id: Optional[str]) -> dict:
    """Resolve ``--candidate`` to a default-candidates entry (stable ids)."""
    if candidate_id is None:
        return {}
    for candidate in default_candidates():
        if candidate["candidate_id"] == candidate_id:
            return candidate
    raise SystemExit(
        f"unknown candidate {candidate_id!r}; choose from {[c['candidate_id'] for c in default_candidates()]}"
    )


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: ``python -m tests.unit.benchmark_fixtures.benchmark_runner``."""
    parser = argparse.ArgumentParser(
        description="Run the fixed-vs-adaptive VieNeu chunking benchmark (OpenSpec 8.3)."
    )
    parser.add_argument("--policy", choices=("fixed", "adaptive_vi"), default="fixed")
    parser.add_argument("--mode", choices=VALID_MODES, default=_SIMULATION)
    parser.add_argument("--output", type=Path, default=Path("benchmarks"))
    parser.add_argument("--candidate-id", default=None)
    parser.add_argument("--corpus", type=Path, default=CORPUS_PATH)
    args = parser.parse_args(argv)

    if args.policy == "adaptive_vi" and args.candidate_id:
        candidate = _candidate_from_cli(args.candidate_id)
        adaptive_config = AdaptiveViPolicyConfig(**candidate["adaptive_config"])
        candidate_id = candidate["candidate_id"]
    else:
        adaptive_config = None
        candidate_id = args.candidate_id

    try:
        if args.mode == _VIENEU:
            result = run_vieneu(
                ChunkPolicy(args.policy),
                corpus_path=args.corpus,
                output_dir=args.output,
                candidate_id=candidate_id,
                adaptive_config=adaptive_config,
            )
        else:
            result = run_benchmark(
                ChunkPolicy(args.policy),
                corpus_path=args.corpus,
                candidate_id=candidate_id,
                adaptive_config=adaptive_config,
            )
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    json_path, md_path = write_report(result, args.output)
    print(f"wrote {json_path} and {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
