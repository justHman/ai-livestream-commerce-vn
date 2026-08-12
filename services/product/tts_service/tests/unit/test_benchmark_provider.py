"""Smoke tests for the direct provider benchmark script (Change T 14.x).

Runs the CLI in fake mode with tiny sample counts and asserts the JSON
output shape, RTF math, and fake-provider call accounting.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "benchmark_provider.py"


def _run_cli(*args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(proc.stdout)


def test_fake_mode_output_shape() -> None:
    payload = _run_cli("--mode", "fake", "--batch-sizes", "1,2", "--samples", "8", "--no-sleep")
    assert payload["config"]["mode"] == "fake"
    assert payload["config"]["hardware"]["python"]
    assert [r["batch_size"] for r in payload["results"]] == [1, 2]
    for result in payload["results"]:
        assert result["items"] == 8
        assert result["wall_seconds"] > 0
        assert result["audio_seconds"] > 0
        assert result["rtf"] > 0
        assert result["realtime_x"] == result["rtf"]
        assert result["items_per_second"] > 0


def test_rtf_matches_audio_over_wall() -> None:
    payload = _run_cli("--mode", "fake", "--batch-sizes", "1", "--samples", "8", "--no-sleep")
    result = payload["results"][0]
    expected = result["audio_seconds"] / result["wall_seconds"]
    assert abs(result["rtf"] - expected) < 1e-2


def test_output_file_written() -> None:
    tmp = Path(__file__).parent / "benchmark_provider_out.json"
    try:
        subprocess.run(
            [sys.executable, str(_SCRIPT), "--mode", "fake", "--batch-sizes", "1",
             "--samples", "4", "--no-sleep", "--output", str(tmp)],
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(tmp.read_text(encoding="utf-8"))["config"]["mode"] == "fake"
    finally:
        tmp.unlink(missing_ok=True)


def test_fake_provider_records_batch_calls() -> None:
    import importlib

    module = importlib.import_module("scripts.benchmark_provider")
    provider = module.AsyncFakeProvider(no_sleep=True)
    provider.batch_calls = 0
    provider.batched_items = 0
    result = module._run_sweep(
        provider,
        batch_size=2,
        samples=8,
        corpus=module.CORPUS,
        voice_profile_id="default",
    )
    assert provider.batch_calls == 4  # 8 items / 2 per batch
    assert provider.batched_items == 8
    assert result[0]["items"] == 8
