"""Smoke tests for the multi-session service benchmark script (Change T 15.x).

Runs the CLI in local fake mode with tiny session counts and asserts the
JSON output shape, zero routing errors (missing tracing headers, cross-session
leaks), and deterministic 429 overload under backpressure.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_multisession.py"
_BASE_ARGS = ("--local", "--mode", "fake", "--sessions", "1,2", "--chunks-per-session", "2")


def _run_cli(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), *_BASE_ARGS, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_same_voice_output_shape() -> None:
    payload = _run_cli("--scenario", "same-voice")
    assert payload["config"]["mode"] == "fake"
    assert payload["config"]["base_url"] == "local-asgi"
    assert [r["scenario"] for r in payload["results"]] == ["same-voice", "same-voice"]
    assert [r["sessions"] for r in payload["results"]] == [1, 2]
    for result in payload["results"]:
        assert result["requests"] == result["sessions"] * 2
        assert result["ok"] == result["requests"]
        assert result["errors"] == 0
        assert result["missing_tracing_headers"] == 0
        assert result["wall_seconds"] > 0
        assert result["audio_seconds"] > 0
        assert result["rtf"] > 0
        assert result["throughput"] == result["rtf"]
        assert set(result["queue_wait_seconds"]) == {"p50", "p95", "p99"}


def test_zero_routing_errors_across_scenarios() -> None:
    payload = _run_cli("--scenario", "burst,dominant-session")
    for result in payload["results"]:
        assert result["ok"] == result["requests"]
        assert result["errors"] == 0
        assert result["error_types"] == {}
        # Routing identity (session/utterance/chunk) survives on every response.
        assert result["missing_tracing_headers"] == 0


def test_backpressure_yields_429() -> None:
    payload = _run_cli("--scenario", "backpressure")
    result = payload["results"][1]  # sessions=2, 2 chunks each = 6 requests
    assert result["errors"] > 0
    assert result["error_types"].get("http_429", 0) == result["errors"]
    assert result["ok"] + result["errors"] == result["requests"]
