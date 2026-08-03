"""Stage 2 benchmark harness contracts."""

from __future__ import annotations

import pytest

from core.director.director import Director
from core.render.orchestrator import StreamOrchestrator
from scripts.benchmark_stage2 import (
    compare_p95,
    run_lanes,
    run_local_real,
    run_offline,
    run_sandbox,
)


def test_offline_lane_runs_production_fsm_and_speech_pipeline(monkeypatch) -> None:
    decide_calls = 0
    pipeline_calls = 0
    original_decide = Director.decide
    original_run = StreamOrchestrator.run
    original_verbatim = StreamOrchestrator.speak_verbatim

    def counted_decide(self, comments, now):
        nonlocal decide_calls
        decide_calls += 1
        return original_decide(self, comments, now)

    async def counted_run(self, session_id, text, system_prompt=None):
        nonlocal pipeline_calls
        pipeline_calls += 1
        return await original_run(self, session_id, text, system_prompt)

    async def counted_verbatim(self, session_id, text):
        nonlocal pipeline_calls
        pipeline_calls += 1
        return await original_verbatim(self, session_id, text)

    monkeypatch.setattr(Director, "decide", counted_decide)
    monkeypatch.setattr(StreamOrchestrator, "run", counted_run)
    monkeypatch.setattr(StreamOrchestrator, "speak_verbatim", counted_verbatim)

    result = run_offline()

    assert result["status"] == "pass"
    assert decide_calls >= 10
    assert pipeline_calls >= 10


def test_offline_lane_reports_required_coverage_and_metrics() -> None:
    result = run_offline()

    assert result["coverage"]["opening_turns"] == 3
    assert result["coverage"]["product_lifecycle"] is True
    assert result["coverage"]["qa_windows"] >= 2
    assert result["coverage"]["qa_answers"] >= 2
    assert result["coverage"]["excursion"] is True
    assert result["coverage"]["pivot_enter"] is True
    assert result["coverage"]["pivot_resume"] is True
    assert result["turns"]
    assert {"action", "stage", "product_id", "revisions", "latency_ms"} <= set(result["turns"][0])
    assert {"queue_peak", "retries", "stale", "drops", "underflow"} <= set(result["queue"])
    assert result["critical_path"]["stage"] in result["p95_latency_ms"]
    assert result["cleanup"]["clean"] is True


def test_local_real_fails_loud_when_real_engines_are_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")

    result = run_local_real(timeout_sec=1)

    assert result["status"] == "fail"
    assert result["errors"] == [{"stage": "configuration", "type": "RuntimeError"}]


def test_all_lanes_skip_sandbox_without_explicit_opt_in() -> None:
    calls: list[str] = []

    def runner(name: str):
        def run() -> dict:
            calls.append(name)
            return {"lane": name, "status": "pass"}

        return run

    result = run_lanes(
        "all",
        allow_sandbox=False,
        runners={
            "offline": runner("offline"),
            "local-real": runner("local-real"),
            "sandbox": runner("sandbox"),
        },
    )

    assert calls == ["offline", "local-real"]
    assert result["sandbox"] == {"lane": "sandbox", "status": "skipped_opt_in_required"}


def test_sandbox_lane_rejects_unbounded_turn_count() -> None:
    with pytest.raises(ValueError, match="1 through 3"):
        run_sandbox(max_turns=4, backend=object())


def test_sandbox_lane_reuses_active_verification_contract() -> None:
    calls = 0

    def transport() -> dict:
        nonlocal calls
        calls += 1
        return {
            "ready": True,
            "layers": [
                {"name": "credentials", "status": "pass", "latency_ms": 1.0},
                {"name": "connectivity", "status": "pass", "latency_ms": 2.0},
                {"name": "speech", "status": "pass", "latency_ms": 3.0},
            ],
        }

    result = run_sandbox(max_turns=2, transport=transport)

    assert result["status"] == "pass"
    assert calls == 2
    assert all(turn["playback_confirmed"] for turn in result["turns"])


def test_sandbox_lane_confirms_bounded_playback_and_cleans_up() -> None:
    class Backend:
        def __init__(self) -> None:
            self.stopped: list[str] = []

        def verify_credentials(self) -> dict:
            return {"credits_available": True}

        def start(self, opts):
            from core.render.base import StartResult

            return StartResult("sandbox-session", "wss://example", "secret-client-token")

        def say(self, session_id: str, text: str, generate: bool = True) -> str:
            return "Phát xong."

        def stop(self, session_id: str) -> None:
            self.stopped.append(session_id)

    backend = Backend()
    result = run_sandbox(max_turns=2, backend=backend)

    assert result["status"] == "pass"
    assert len(result["turns"]) == 2
    assert all(turn["playback_confirmed"] for turn in result["turns"])
    assert backend.stopped == ["sandbox-session"]
    assert "secret-client-token" not in str(result)


def test_regression_gate_uses_p95_samples_and_ignores_zero_baseline() -> None:
    result = compare_p95(
        {"latency_samples_ms": {"llm": [100, 121], "tts": [10]}},
        {"latency_samples_ms": {"llm": [100, 100], "tts": [0]}},
    )

    assert result["status"] == "fail"
    assert result["regressions"]["llm"]["regression"] == 0.21
    assert "tts" not in result["regressions"]
