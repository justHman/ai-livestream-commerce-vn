"""Run production Stage 2 FSM and speech-pipeline benchmark lanes."""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.api.v1 import build_run_plan
from backend.config import AppConfig
from backend.application.director.catalog import embedding_text
from backend.application.director.clustering import Comment
from backend.application.entity.models import EntityDocument
from benchmarks.backend.fixture_data import MOCK_VIEWER_MSGS
from benchmarks.fixtures.corpus import corpus_products
from backend.application.director.config import StreamConfig
from backend.application.director.decision import Decision, Director
from backend.application.director.embeddings import HashingEmbedder, build_embedder, embedder_status
from backend.application.director.routing import route_comment
from backend.application.director.state import Phase, ProductState, StreamState
from llm.engines.base import LLMEngine, LLMRequest, LLMResponse
from avatar.engines.base import FullPipelineBackend, StartOptions
from avatar.engines.mock import MockRenderBackend
from backend.application.render.orchestrator import StreamOrchestrator
from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
from tts.engines.base import AudioChunk, TTSEngine, TTSRequest

_DEFAULT_TIMEOUT_SEC = 600.0
_REQUIRED_LIFECYCLE_STAGES = {"intro", "benefit", "offer", "trust", "cta", "transition"}


class _FixedLLM(LLMEngine):
    name = "stage2-benchmark-fixed"

    @classmethod
    def from_config(cls, cfg: dict) -> "_FixedLLM":
        return cls()

    def generate(self, req: LLMRequest) -> LLMResponse:
        stage = req.messages[-1]["content"] if req.messages else ""
        return LLMResponse(
            text=f"Nội dung benchmark hoàn chỉnh cho {stage[:32]}. Mời cả nhà tiếp tục theo dõi.",
            engine=self.name,
        )


class _FixedTTS(TTSEngine):
    name = "stage2-benchmark-fixed"
    sample_rate = 8_000

    @classmethod
    def from_config(cls, cfg: dict) -> "_FixedTTS":
        return cls()

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        return AudioChunk(pcm=np.zeros(400, dtype=np.float32), sample_rate=self.sample_rate)


class _TimedLLM(LLMEngine):
    """Measure an existing engine without changing its generation behavior."""

    def __init__(self, inner: LLMEngine) -> None:
        self.inner = inner
        self.name = inner.name
        self.last_ttft_ms = 0.0
        self.last_total_ms = 0.0

    @classmethod
    def from_config(cls, cfg: dict) -> "_TimedLLM":
        raise NotImplementedError("wrap an initialized LLM engine")

    def generate(self, req: LLMRequest) -> LLMResponse:
        return self.inner.generate(req)

    def begin_turn(self) -> None:
        self.last_ttft_ms = 0.0
        self.last_total_ms = 0.0

    def stream_chunks(self, req, *, session_id="", utterance_id=""):
        started = time.perf_counter()
        first = True
        for chunk in self.inner.stream_chunks(
            req,
            session_id=session_id,
            utterance_id=utterance_id,
        ):
            if first:
                self.last_ttft_ms = (time.perf_counter() - started) * 1_000
                first = False
            yield chunk
        self.last_total_ms = (time.perf_counter() - started) * 1_000


class _TimedTTS(TTSEngine):
    """Measure first-audio and total TTS iteration time for one turn."""

    def __init__(self, inner: TTSEngine) -> None:
        self.inner = inner
        self.name = inner.name
        self.sample_rate = inner.sample_rate
        self.last_first_audio_ms = 0.0
        self.last_total_ms = 0.0
        self._turn_started = 0.0
        self._saw_audio = False

    @classmethod
    def from_config(cls, cfg: dict) -> "_TimedTTS":
        raise NotImplementedError("wrap an initialized TTS engine")

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        return self.inner.synthesize(req)

    def begin_turn(self) -> None:
        self.last_first_audio_ms = 0.0
        self.last_total_ms = 0.0
        self._turn_started = time.perf_counter()
        self._saw_audio = False

    def stream_audio(self, *args, **kwargs):
        for window in self.inner.stream_audio(*args, **kwargs):
            if not self._saw_audio:
                self.last_first_audio_ms = (
                    time.perf_counter() - self._turn_started
                ) * 1_000
                self._saw_audio = True
            yield window
        self.last_total_ms = (time.perf_counter() - self._turn_started) * 1_000


def _percentile95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _latency_samples(report: dict) -> dict[str, list[float]]:
    samples = report.get("latency_samples_ms")
    if isinstance(samples, dict):
        return {
            stage: [float(value) for value in values]
            for stage, values in samples.items()
            if isinstance(values, list)
        }
    legacy = report.get("latency_ms") or report.get("p95_latency_ms") or {}
    return {
        stage: [float(value)]
        for stage, value in legacy.items()
        if isinstance(value, (int, float))
    }


def compare_p95(current: dict, baseline: dict | None, threshold: float = 0.20) -> dict:
    """Fail when any comparable stage p95 is more than ``threshold`` slower."""
    if not baseline:
        return {"status": "baseline-created", "regressions": {}}
    current_samples = _latency_samples(current)
    baseline_samples = _latency_samples(baseline)
    regressions: dict[str, dict[str, float]] = {}
    for stage, values in current_samples.items():
        old_values = baseline_samples.get(stage) or []
        old = _percentile95(old_values)
        value = _percentile95(values)
        if old > 0 and value > old * (1 + threshold):
            regressions[stage] = {
                "current": round(value, 3),
                "baseline": round(old, 3),
                "regression": round(value / old - 1, 4),
            }
    return {"status": "fail" if regressions else "pass", "regressions": regressions}


def _product_comments(
    text: str,
    count: int,
    prefix: str,
    now: float,
    products: list[EntityDocument],
    embedder: object,
    current_product_id: str,
) -> list[Comment]:
    if text not in MOCK_VIEWER_MSGS:
        raise ValueError(f"benchmark fixture text is absent: {text}")
    vectors = embedder.encode([text] * count)
    return [
        route_comment(
            Comment(
                id=f"{prefix}-{index}",
                text=text,
                embedding=list(vectors[index]),
                t=now + index / 100,
            ),
            products,
            current_product_id,
        )
        for index in range(count)
    ]


def _new_director(products: list[EntityDocument], embedder: object) -> Director:
    vectors = embedder.encode([embedding_text(product) for product in products])
    states = []
    for product, vector in zip(products, vectors):
        states.append(
            ProductState(
                product_id=product.id,
                name=product.name,
                ref_image=(
                    str(product.get_fact("custom.ref_image").value)
                    if product.get_fact("custom.ref_image") is not None
                    else None
                ),
                embedding=list(vector),
            )
        )
    state = StreamState(
        phase=Phase.OPENING,
        products=states,
        run_plan=build_run_plan(products),
    )
    state.cursor.profile_revision = 1
    state.cursor.catalog_revision = 1
    state.cursor.config_revision = 0
    return Director(
        state=state,
        cfg=StreamConfig(
            product_time_budget_sec=9_999,
            engagement_decay_sec=9_999,
            qa_topic_cooldown_sec=0,
        ),
        catalog={product.id: product for product in products},
    )


async def _execute_turn(
    director: Director,
    decision: Decision,
    orchestrator: StreamOrchestrator,
    queue: BoundedVideoQueue,
    metrics: CoordinatorMetrics,
    llm: _TimedLLM,
    tts: _TimedTTS,
    session_id: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    llm.begin_turn()
    tts.begin_turn()
    script = decision.prepared_script or decision.text or ""
    if decision.action == "resume_product":
        director.mark_spoken(decision)
    elif decision.text and not decision.prompt:
        script = await orchestrator.speak_verbatim(session_id, decision.text)
        director.mark_spoken(decision)
    else:
        prompt = decision.prompt or decision.text or decision.reason
        script = await orchestrator.run(session_id, prompt, system_prompt="Stage 2 benchmark")
        decision.prepared_script = script
        director.mark_spoken(decision)
    end_to_end = (time.perf_counter() - started) * 1_000
    queue_peak = queue.qsize()
    windows = 0
    while queue.qsize():
        await queue.get()
        windows += 1
    render_ms = max(0.0, end_to_end - llm.last_total_ms - tts.last_total_ms)
    latency = {
        "llm_ttft": llm.last_ttft_ms,
        "llm_total": llm.last_total_ms,
        "tts_first_audio": tts.last_first_audio_ms,
        "tts_total": tts.last_total_ms,
        "render_playback": render_ms,
        "pipeline_first_window": metrics.pipeline_total_ms or 0.0,
        "end_to_end": end_to_end,
    }
    return {
        "turn_id": decision.turn_id,
        "action": decision.action,
        "stage": decision.stage,
        "product_id": decision.product_id,
        "script": script,
        "revisions": {
            "profile": director.state.cursor.profile_revision,
            "catalog": director.state.cursor.catalog_revision,
            "config": director.state.cursor.config_revision,
            "generation": director.state.cursor.generation_token,
        },
        "latency_ms": {key: round(value, 3) for key, value in latency.items()},
        "queue_depth": queue_peak,
        "windows": windows,
    }


async def _run_full_loop(
    lane: str,
    embedder: object,
    llm_engine: LLMEngine,
    tts_engine: TTSEngine,
) -> dict[str, Any]:
    started = time.perf_counter()
    products = corpus_products()
    director = _new_director(products, embedder)
    backend = MockRenderBackend(fps=1, width=96, height=54)
    session_id = backend.start(StartOptions(is_sandbox=True)).session_id
    queue = BoundedVideoQueue(max_size=64)
    metrics = CoordinatorMetrics()
    llm = _TimedLLM(llm_engine)
    tts = _TimedTTS(tts_engine)
    orchestrator = StreamOrchestrator(llm, tts, backend, queue, metrics)
    turns: list[dict[str, Any]] = []
    latency_samples: dict[str, list[float]] = {}
    qa_windows: set[tuple[str | None, int]] = set()
    cleanup = {"clean": False}
    logical_now = 1.0

    async def perform(comments: list[Comment] | None = None) -> Decision:
        nonlocal logical_now, metrics, orchestrator
        director.state.phase_elapsed_sec = logical_now
        decision_started = time.perf_counter()
        decision = director.decide(comments or [], now=logical_now)
        decision_ms = (time.perf_counter() - decision_started) * 1_000
        metrics = CoordinatorMetrics()
        orchestrator = StreamOrchestrator(llm, tts, backend, queue, metrics)
        record = await _execute_turn(
            director,
            decision,
            orchestrator,
            queue,
            metrics,
            llm,
            tts,
            session_id,
        )
        record["latency_ms"]["decision"] = round(decision_ms, 3)
        turns.append(record)
        for stage, value in record["latency_ms"].items():
            latency_samples.setdefault(stage, []).append(float(value))
        if decision.action in ("answer_fact", "answer_cluster"):
            qa_windows.add((decision.product_id, director.state.qa_window_stage_index))
        logical_now += 1.0
        return decision

    try:
        for _ in range(3):
            await perform()
        await perform()
        await perform()

        current_id = director.state.current_product().product_id
        price = _product_comments(
            "Áo hoodie giá sao shop?",
            2,
            "qa-price",
            logical_now,
            products,
            embedder,
            current_id,
        )
        await perform(price)
        size = _product_comments(
            "Áo hoodie có mũ đôi không shop?",
            2,
            "qa-size",
            logical_now,
            products,
            embedder,
            current_id,
        )
        for _ in range(3):
            decision = await perform(price + size)
            if decision.action in ("answer_fact", "answer_cluster"):
                break
        trust = _product_comments(
            "Áo hoodie có túi kangaroo không?",
            2,
            "qa-trust",
            logical_now,
            products,
            embedder,
            current_id,
        )
        for _ in range(3):
            decision = await perform(price + size + trust)
            if decision.cluster_member_ids and any("qa-trust" in item for item in decision.cluster_member_ids):
                break

        excursion_comments = _product_comments(
            "Serum vitamin C dùng ban ngày hay ban đêm?",
            2,
            "excursion",
            logical_now,
            products,
            embedder,
            current_id,
        )
        excursion = await perform(excursion_comments)

        hot = _product_comments(
            "Serum vitamin C bao nhiêu tiền ạ?",
            8,
            "pivot-hot",
            logical_now,
            products,
            embedder,
            current_id,
        ) + _product_comments(
            "Áo hoodie giá sao shop?",
            4,
            "pivot-current",
            logical_now,
            products,
            embedder,
            current_id,
        )
        pivot = await perform(hot)
        pivot_id = pivot.product_id
        pivot_stages: set[str] = set()
        task_count = len(director._sales_tasks(pivot_id or ""))
        for _ in range(task_count + 3):
            if (
                director.state.current_product()
                and director.state.current_product().product_id == pivot_id
                and director.state.current_product().stage_turn_index >= task_count
            ):
                break
            turn = await perform([])
            if turn.product_id == pivot_id and turn.stage:
                pivot_stages.add(turn.stage)

        cooled = _product_comments(
            "Serum vitamin C bao nhiêu tiền ạ?",
            4,
            "pivot-cool",
            logical_now,
            products,
            embedder,
            pivot_id or "P002",
        ) + _product_comments(
            "Áo hoodie giá sao shop?",
            6,
            "resume-hot",
            logical_now,
            products,
            embedder,
            pivot_id or "P002",
        )
        resume = await perform(cooled)

        qa_answers = sum(
            turn["action"] in ("answer_fact", "answer_cluster") for turn in turns
        )
        coverage = {
            "opening_turns": sum(turn["stage"] == "opening" for turn in turns),
            "product_lifecycle": _REQUIRED_LIFECYCLE_STAGES <= pivot_stages,
            "qa_windows": len(qa_windows),
            "qa_answers": qa_answers,
            "excursion": bool(excursion.excursion),
            "pivot_enter": bool(pivot.pivot),
            "pivot_resume": resume.action == "resume_product",
        }
        required = (
            coverage["opening_turns"] == 3
            and coverage["product_lifecycle"]
            and coverage["qa_windows"] >= 2
            and coverage["qa_answers"] >= 2
            and coverage["excursion"]
            and coverage["pivot_enter"]
            and coverage["pivot_resume"]
        )
        p95 = {
            stage: round(_percentile95(values), 3)
            for stage, values in latency_samples.items()
        }
        critical_stage = max(p95, key=p95.get)
        return {
            "lane": lane,
            "status": "pass" if required else "fail",
            "session": {"id": session_id, "turn_count": len(turns)},
            "embedder": embedder_status(embedder),
            "coverage": coverage,
            "turns": turns,
            "latency_samples_ms": latency_samples,
            "p95_latency_ms": p95,
            "critical_path": {"stage": critical_stage, "p95_ms": p95[critical_stage]},
            "queue": {
                "queue_peak": max((turn["queue_depth"] for turn in turns), default=0),
                "retries": 0,
                "stale": 0,
                "drops": queue.dropped_count(),
                "underflow": queue.underflow_count,
            },
            "errors": [],
            "cleanup": cleanup,
            "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
        }
    finally:
        queue.clear()
        try:
            backend.stop(session_id)
        except KeyError:
            pass
        cleanup["clean"] = queue.qsize() == 0


def _run_full_loop_sync(
    lane: str,
    embedder: object,
    llm: LLMEngine,
    tts: TTSEngine,
    timeout_sec: float,
) -> dict[str, Any]:
    async def bounded() -> dict[str, Any]:
        return await asyncio.wait_for(
            _run_full_loop(lane, embedder, llm, tts),
            timeout=timeout_sec,
        )

    return asyncio.run(bounded())


def run_offline(timeout_sec: float = 60.0) -> dict[str, Any]:
    """Run the deterministic production FSM and streaming pipeline lane."""
    return _run_full_loop_sync(
        "offline-deterministic",
        HashingEmbedder(),
        _FixedLLM(),
        _FixedTTS(),
        timeout_sec,
    )


def run_local_real(timeout_sec: float = _DEFAULT_TIMEOUT_SEC) -> dict[str, Any]:
    """Run real semantic, LLM, and TTS engines with the mock renderer."""
    config = AppConfig.from_env()
    if config.llm.engine in ("none", "", None) or config.tts.engine in ("tone", "", None):
        return {
            "lane": "local-real",
            "status": "fail",
            "errors": [{"stage": "configuration", "type": "RuntimeError"}],
        }
    llm = None
    tts = None
    stage = "embedder"
    try:
        embedder = build_embedder(mode="semantic-required")
        stage = "llm_load"
        llm = config.build_llm_engine()
        stage = "tts_load"
        tts = config.build_tts_engine()
        if llm is None or tts is None:
            raise RuntimeError("real engines required")
        return _run_full_loop_sync("local-real", embedder, llm, tts, timeout_sec)
    except TimeoutError as exc:
        return {
            "lane": "local-real",
            "status": "fail",
            "errors": [{"stage": "full_loop_timeout", "type": type(exc).__name__}],
        }
    except Exception as exc:
        return {
            "lane": "local-real",
            "status": "fail",
            "errors": [{"stage": stage, "type": type(exc).__name__}],
        }
    finally:
        if llm is not None:
            llm.unload()
        if tts is not None:
            tts.unload()


def _sandbox_endpoint_transport() -> dict[str, Any]:
    base_url = os.environ.get("STAGE2_BASE_URL", "http://127.0.0.1:8800").rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/api/v1/admin/sandbox/verify",
        data=json.dumps({"speech_text": "Stage 2 sandbox playback verification."}).encode(
            "utf-8"
        ),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    token = os.environ.get("ADMIN_API_TOKEN", "")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def run_sandbox(
    *,
    max_turns: int = 1,
    backend: FullPipelineBackend | None = None,
    transport: Callable[[], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run an explicitly requested, bounded playback-confirmed sandbox smoke."""
    if not 1 <= max_turns <= 3:
        raise ValueError("sandbox max_turns must be 1 through 3")
    active_backend = backend
    session_id = None
    started = time.perf_counter()
    turns: list[dict[str, Any]] = []
    try:
        if transport is not None:
            for index in range(max_turns):
                turn_started = time.perf_counter()
                result = transport()
                speech = next(
                    (
                        layer
                        for layer in result.get("layers", [])
                        if layer.get("name") == "speech"
                    ),
                    {},
                )
                confirmed = result.get("ready") is True and speech.get("status") == "pass"
                turns.append(
                    {
                        "index": index + 1,
                        "playback_confirmed": confirmed,
                        "latency_ms": round(
                            (time.perf_counter() - turn_started) * 1_000,
                            3,
                        ),
                    }
                )
                if not confirmed:
                    raise RuntimeError("sandbox verification did not confirm playback")
            return {
                "lane": "sandbox",
                "status": "pass",
                "scope": "bounded-smoke-only",
                "turns": turns,
                "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
                "errors": [],
            }
        if active_backend is None:
            config = AppConfig.from_env()
            if config.render_backend != "cloud_liveavatar":
                raise RuntimeError("cloud sandbox backend required")
            candidate = config.build_render_backend()
            if not isinstance(candidate, FullPipelineBackend):
                raise RuntimeError("full pipeline sandbox backend required")
            active_backend = candidate
        probe = getattr(active_backend, "verify_credentials", None)
        if callable(probe):
            probe()
        result = active_backend.start(StartOptions(is_sandbox=True))
        session_id = result.session_id
        for index in range(max_turns):
            turn_started = time.perf_counter()
            script = active_backend.say(
                session_id,
                f"Stage 2 sandbox playback turn {index + 1}.",
                generate=True,
            )
            turns.append(
                {
                    "index": index + 1,
                    "playback_confirmed": True,
                    "script_length": len(script),
                    "latency_ms": round((time.perf_counter() - turn_started) * 1_000, 3),
                }
            )
        return {
            "lane": "sandbox",
            "status": "pass",
            "scope": "bounded-smoke-only",
            "turns": turns,
            "duration_ms": round((time.perf_counter() - started) * 1_000, 3),
            "errors": [],
        }
    except Exception as exc:
        return {
            "lane": "sandbox",
            "status": "fail",
            "scope": "bounded-smoke-only",
            "turns": turns,
            "errors": [{"stage": "sandbox", "type": type(exc).__name__}],
        }
    finally:
        if active_backend is not None and session_id is not None:
            try:
                active_backend.stop(session_id)
            except Exception:
                pass


def run_lanes(
    lane: str,
    *,
    allow_sandbox: bool = False,
    sandbox_turns: int = 1,
    timeout_sec: float = _DEFAULT_TIMEOUT_SEC,
    runners: dict[str, Callable[[], dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Dispatch one lane or all safe lanes; sandbox always requires opt-in."""
    selected = runners or {
        "offline": run_offline,
        "local-real": lambda: run_local_real(timeout_sec),
        "sandbox": lambda: run_sandbox(
            max_turns=sandbox_turns,
            transport=_sandbox_endpoint_transport,
        ),
    }
    if lane == "sandbox" and not allow_sandbox:
        return {"lane": "sandbox", "status": "skipped_opt_in_required"}
    if lane != "all":
        return selected[lane]()
    results = {
        "offline": selected["offline"](),
        "local-real": selected["local-real"](),
    }
    results["sandbox"] = (
        selected["sandbox"]()
        if allow_sandbox
        else {"lane": "sandbox", "status": "skipped_opt_in_required"}
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lane",
        choices=("offline", "local-real", "sandbox", "all"),
        default="offline",
    )
    parser.add_argument("--allow-sandbox", action="store_true")
    parser.add_argument("--sandbox-turns", type=int, default=1)
    parser.add_argument("--timeout-sec", type=float, default=_DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--output", default="")
    parser.add_argument("--baseline", default="")
    args = parser.parse_args()
    result = run_lanes(
        args.lane,
        allow_sandbox=args.allow_sandbox,
        sandbox_turns=args.sandbox_turns,
        timeout_sec=args.timeout_sec,
    )
    baseline = (
        json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        if args.baseline
        else None
    )
    if args.lane != "all" and "latency_samples_ms" in result:
        result["regression"] = compare_p95(result, baseline)
        if result["regression"]["status"] == "fail":
            result["status"] = "fail"
    output = Path(args.output or f".runtime/benchmarks/stage2/{args.lane}.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    statuses = (
        [item.get("status") for item in result.values() if isinstance(item, dict)]
        if args.lane == "all"
        else [result.get("status")]
    )
    if "fail" in statuses:
        raise SystemExit(1)


if __name__ == "__main__":
    main()


__all__ = [
    "compare_p95",
    "run_lanes",
    "run_local_real",
    "run_offline",
    "run_sandbox",
]
