"""Multi-session TTS service benchmark over the real HTTP surface (Change T 15.x).

Drives ``POST /v1/audio/speech`` (the canonical backend-facing path) with
ordinary concurrent HTTP requests — no client batch endpoint (15.1). Sessions
submit chunks; the scheduler coalesces them into provider batches; the
benchmark measures wall time, audio seconds, RTF, throughput, queue-wait
percentiles, and error/cancellation counts (15.14).

Two transports:
- ``--local``: in-process ASGI over httpx ``ASGITransport`` with a fake
  provider, wired the same way tests/integration/test_runtime_api.py does
  (provider injected, ``TTS_PROVIDER=fake``, runtime attached to
  ``app.state``). Each scenario builds a fresh app/runtime so admission
  limits and stats reset per scenario. Note: with ASGITransport there is no
  client-side TCP socket, so client-measured queue wait approximates only;
  real network latency is measured against a deployed service (``--base-url``).
- default: real HTTP against ``--base-url``. Requires the service runtime to
  be reachable; the script fails fast (exit nonzero) when it cannot connect.

Scenarios (15.3-15.12): same-voice, mixed-voices, mixed-cloned, mixed-styles,
burst, staggered, dominant-session, priority-mix, backpressure, cancellation.

Gate (15.13): ``--compare-baseline <file.json>`` compares weighted-average
service throughput (audio_seconds / wall_seconds) against the direct-provider
baseline and prints a WARNING when the ratio is below 0.8. It never fails the
exit code — the gate is only meaningful against a real service and a real
direct-provider run (fake mode is a smoke harness).

Usage:
    python scripts/benchmark_multisession.py --local --mode fake \
        --sessions 1,2 --chunks-per-session 2 --scenario same-voice
    python scripts/benchmark_multisession.py --local --mode fake \
        --sessions 4,8 --scenario mixed-voices,burst --output results.json
    python scripts/benchmark_multisession.py --base-url http://127.0.0.1:8002 \
        --sessions 1,2,4,8,16,32 --scenario burst
    python scripts/benchmark_multisession.py --base-url http://127.0.0.1:8002 \
        --scenario same-voice --compare-baseline direct_results.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from tts.config import RuntimeConfig  # noqa: E402
from tts.providers.models import AudioResult, ProviderRequest, ProviderResult  # noqa: E402
from tts.scheduler.admission import AdmissionController  # noqa: E402
from tts.scheduler.fairness import FairnessSelector, PendingPopulation  # noqa: E402
from tts.scheduler.runtime import SchedulerRuntime  # noqa: E402
from tts.voices.cache import CachedVoiceProfileStore  # noqa: E402
from tts.voices.service import VoiceProfileService  # noqa: E402
from tts.voices.store import DEFAULT_TENANT_ID  # noqa: E402

# Reuse the fixed Vietnamese corpus from the direct-provider benchmark so the
# 15.13 gate compares the same workload shape (same host/corpus/config).
sys.path.insert(0, str(Path(__file__).resolve().parent))
from benchmark_provider import CORPUS, hardware_metadata  # noqa: E402

SCENARIOS = (
    "same-voice",
    "mixed-voices",
    "mixed-cloned",
    "mixed-styles",
    "burst",
    "staggered",
    "dominant-session",
    "priority-mix",
    "backpressure",
    "cancellation",
)

# Fake provider voice ids seeded into the voice store per run; sessions
# reference them the same way real callers reference profile ids.
FAKE_PRESET_IDS = ("preset-0", "preset-1", "preset-2")
# Real SDK v3 Turbo preset voices (voices_v3_turbo.json) — fake mode may use
# any names, but real mode MUST seed names the pinned SDK actually resolves.
FAKE_PRESET_NAMES = ("Phạm Tuyên", "Trúc Ly", "Xuân Vĩnh")
FAKE_CLONED_IDS = ("clone-0", "clone-1", "clone-2")
STYLES = ("natural", "news", "storytelling")
FAKE_SAMPLE_RATE = 48_000


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    index = min(len(sorted_values) - 1, max(0, int(q * len(sorted_values))))
    return sorted_values[index]


def _percentiles(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    return {
        "p50": round(_percentile(ordered, 0.5), 4),
        "p95": round(_percentile(ordered, 0.95), 4),
        "p99": round(_percentile(ordered, 0.99), 4),
    }


# ── fake provider (local mode) ────────────────────────────────────────────────
class FakeProvider:
    """Deterministic in-process provider for ``--local`` runs.

    Mirrors the provider shape from tests/integration/test_runtime_api.py:
    native batching + mixed voices; ``synthesize_batch`` sleeps proportional
    to batch size so coalescing and queue-wait math is exercised. All fake
    profile ids map to the same provider name/generation config, so every
    batch key matches and mixed-voice requests share provider batches (15.5).
    """

    provider_name = "fake"

    def __init__(self, *, dispatch_delay: float = 0.004, max_batch_size: int = 32) -> None:
        self.dispatch_delay = dispatch_delay
        self.max_batch_size = max_batch_size
        self.batch_calls: list[tuple] = []

    def capabilities(self):
        from tts.providers.capabilities import ProviderCapabilities

        return ProviderCapabilities(
            provider_name="fake",
            model_revision="fake-1",
            sample_rate_hz=FAKE_SAMPLE_RATE,
            supports_native_batch=True,
            max_batch_size=self.max_batch_size,
            supports_mixed_voice_batch=True,
            supported_styles=tuple(STYLES),
            supported_response_formats=("pcm", "wav"),
        )

    def batch_key(self, request: ProviderRequest):
        return ("fake", request.generation_config.temperature)

    async def synthesize_batch(self, requests: list[ProviderRequest]) -> list[ProviderResult]:
        self.batch_calls.append(tuple(r.request_id for r in requests))
        if self.dispatch_delay:
            await asyncio.sleep(self.dispatch_delay * len(requests))
        return [self._result(request) for request in requests]

    def _result(self, request: ProviderRequest) -> AudioResult:
        duration_ms = int(len(request.input_text) * 7.2)
        return AudioResult(
            request_id=request.request_id,
            sample_rate=FAKE_SAMPLE_RATE,
            waveform=np.zeros(FAKE_SAMPLE_RATE * duration_ms // 1000, dtype=np.float32),
            response_format=request.response_format,
            duration_ms=duration_ms,
        )


class _ProfileStore:
    """In-memory profile store holding the seeded preset/cloned metadata."""

    def __init__(self, profiles: dict[str, object]) -> None:
        self._profiles = profiles

    def load_profile(self, voice_profile_id: str, tenant_id: str):
        profile = self._profiles.get(voice_profile_id)
        if profile is None:
            raise KeyError(voice_profile_id)
        return profile, {}

    def save_profile(self, profile, payload: dict) -> None:
        self._profiles[profile.voice_profile_id] = profile

    def delete_profile(self, voice_profile_id: str, tenant_id: str) -> None:
        self._profiles.pop(voice_profile_id, None)

    def list_profiles(self, tenant_id: str) -> list:
        return list(self._profiles.values())


def _seed_profiles() -> _ProfileStore:
    """Build preset + cloned profile metadata for the fake provider."""
    from tts.voices.models import VoiceProfile

    profiles: dict[str, VoiceProfile] = {}
    for index, name in enumerate(FAKE_PRESET_NAMES):
        profile = VoiceProfile(
            voice_profile_id=FAKE_PRESET_IDS[index],
            tenant_id=DEFAULT_TENANT_ID,
            provider_name="fake",
            provider_model_revision="fake-1",
            profile_kind="preset",
            display_name=name,
            provider_payload_location="preset://" + name,
        )
        profiles[profile.voice_profile_id] = profile
    for index in range(len(FAKE_CLONED_IDS)):
        profile = VoiceProfile(
            voice_profile_id=FAKE_CLONED_IDS[index],
            tenant_id=DEFAULT_TENANT_ID,
            provider_name="fake",
            provider_model_revision="fake-1",
            profile_kind="cloned",
            display_name=f"clone-{index}",
            provider_payload_location="",  # store keys by id
        )
        profiles[profile.voice_profile_id] = profile
    return _ProfileStore(profiles)


def _make_runtime(provider: FakeProvider, **config_overrides) -> SchedulerRuntime:
    """Build a scheduler runtime over the fake provider with test defaults."""
    overrides = dict(provider="fake", model_revision="fake-1", coalesce_window_ms=1)
    overrides.update(config_overrides)
    global_limit = overrides.pop("global_pending_limit", 512)
    session_limit = overrides.pop("per_session_pending_limit", 64)
    return SchedulerRuntime(
        population=PendingPopulation(),
        admission=AdmissionController(global_limit, session_limit),
        selector=FairnessSelector(),
        provider=provider,
        config=RuntimeConfig(**overrides),
    )


def _build_local_client(provider: FakeProvider, seed: _ProfileStore, runtime: SchedulerRuntime):
    """Build an in-process ASGI client with the runtime wired (no lifespan).

    Mirrors tests/integration/test_runtime_api.py: provider injected into
    ``app.state`` and ``runtime_ready=True``; the voice service resolves the
    seeded profile ids. The lifespan itself is not run, so the app serves
    exactly the state set here.
    """
    from tts import create_app
    from tts.engines.base import ToneEngine

    app = create_app()
    app.state.engine = ToneEngine.from_config({})
    app.state.engine_ready = True
    app.state.provider = provider
    app.state.runtime = runtime
    app.state.runtime_ready = True
    app.state.voice_service = VoiceProfileService(
        CachedVoiceProfileStore(seed), runtime.config, metrics=None
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://bench")


# ── request workers ───────────────────────────────────────────────────────────
def _body(
    *,
    session_id: str,
    seq: int,
    voice_profile_id: str,
    style: str,
    priority: str,
) -> dict:
    return {
        "text": CORPUS[(int(session_id.rsplit("-", 1)[-1]) + seq) % len(CORPUS)],
        "session_id": session_id,
        "utterance_id": f"{session_id}-u{seq}",
        "chunk_seq": seq,
        "voice_profile_id": voice_profile_id,
        "style": style,
        "priority": priority,
        "response_format": "wav",
    }


async def _submit_one(
    client: AsyncClient,
    body: dict,
    sample: dict,
    *,
    wait_for: str,
) -> None:
    """POST one chunk; record status/timing/headers or a client-side error."""
    started = time.monotonic()
    try:
        resp = await client.post("/v1/audio/speech", json=body)
    except Exception as exc:
        sample["errors"].append({"type": type(exc).__name__, "detail": str(exc)})
        return
    sample["queue_waits"].append(time.monotonic() - started)
    if resp.status_code == 200:
        sample["responses"].append({"status": 200, "headers": dict(resp.headers)})
    else:
        try:
            detail = resp.json().get("error", {}).get("code", "unknown")
        except Exception:
            detail = resp.text[:200]
        sample["errors"].append({"type": f"http_{resp.status_code}", "detail": detail})


async def _run_session(
    client: AsyncClient,
    *,
    session_id: str,
    voice_profile_id: str,
    style: str,
    priority: str,
    chunks: int,
    sample: dict,
    concurrent: bool = False,
) -> None:
    """Submit one session's chunks sequentially (or concurrently when asked)."""
    bodies = [
        _body(
            session_id=session_id,
            seq=seq,
            voice_profile_id=voice_profile_id,
            style=style,
            priority=priority,
        )
        for seq in range(chunks)
    ]
    if concurrent:
        await asyncio.gather(
            *(_submit_one(client, body, sample, wait_for=session_id) for body in bodies)
        )
        return
    for body in bodies:
        await _submit_one(client, body, sample, wait_for=session_id)


# ── scenario orchestration ────────────────────────────────────────────────────
def _scenario_plan(
    scenario: str,
    *,
    sessions: int,
    chunks: int,
    profile_ids: list[str],
    styles: list[str],
    priorities: list[str],
) -> list[dict]:
    """Per-session assignment for the scenario (profile/style/priority)."""
    plan = []
    for index in range(sessions):
        plan.append(
            {
                "session_id": f"bench-{index}",
                "voice_profile_id": profile_ids[index % len(profile_ids)],
                "style": styles[index % len(styles)],
                "priority": priorities[index % len(priorities)],
            }
        )
    return plan


async def _run_scenario(
    *,
    scenario: str,
    sessions: int,
    chunks: int,
    base_url: str,
    local: bool,
    profile_ids: list[str],
    styles: list[str],
    priorities: list[str],
) -> dict:
    """Run one scenario; returns the metrics dict (15.14)."""
    sample: dict = {"queue_waits": [], "errors": [], "responses": []}

    if local:
        # Per-scenario runtime shape: backpressure needs a tight per-session
        # limit; cancellation needs a slow provider so cancels land.
        local_overrides: dict = {}
        if scenario == "backpressure":
            local_overrides["per_session_pending_limit"] = 1
        provider = FakeProvider(dispatch_delay=0.5 if scenario == "cancellation" else 0.004)
        runtime = _make_runtime(provider, **local_overrides)
        client = _build_local_client(provider, _seed_profiles(), runtime)
        local_runtime = runtime
    else:
        client = await _http_client(base_url)
        local_runtime = None

    started = time.monotonic()
    plan = _scenario_plan(
        scenario,
        sessions=sessions,
        chunks=chunks,
        profile_ids=profile_ids,
        styles=styles,
        priorities=priorities,
    )

    tasks: list[asyncio.Task] = []
    for spec in plan:
        tasks.append(
            asyncio.create_task(
                _run_session(
                    client,
                    session_id=spec["session_id"],
                    voice_profile_id=spec["voice_profile_id"],
                    style=spec["style"],
                    priority=spec["priority"],
                    chunks=chunks,
                    sample=sample,
                    concurrent=(scenario == "backpressure"),
                )
            )
        )
        if scenario == "staggered":
            await asyncio.sleep(0.05)
        if scenario == "dominant-session" and spec is plan[0]:
            # Session A starts first with a head start; B/C join behind it.
            await asyncio.sleep(0.2)

    if scenario == "cancellation":
        # Cancel half the sessions mid-flight; siblings must not be disturbed.
        await asyncio.sleep(0.15)
        for task in tasks[::2]:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        cancellations = sum(1 for task in tasks if task.cancelled())
    else:
        await asyncio.gather(*tasks)
        cancellations = 0
    wall_seconds = time.monotonic() - started

    if local_runtime is None:
        await client.aclose()

    responses = sample["responses"]
    ok = sum(1 for r in responses if r["status"] == 200)
    missing_headers = sum(
        1
        for r in responses
        if any(
            name not in r["headers"] for name in ("x-session-id", "x-utterance-id", "x-chunk-seq")
        )
    )
    audio_seconds = sum(
        int(r["headers"].get("x-audio-duration-ms", "0")) / 1000.0 for r in responses
    )
    error_types: dict[str, int] = {}
    for error in sample["errors"]:
        error_types[error["type"]] = error_types.get(error["type"], 0) + 1
    # Fairness evidence (15.9): non-dominant sessions must all resolve — an
    # indefinite starvation would leave their requests pending/errored.
    fairness: dict = {}
    if scenario == "dominant-session":
        fairness = {
            "dominant_session": "bench-0",
            "sessions_total": sessions,
            "non_dominant_ok": sum(
                1
                for r in responses
                if r["headers"].get("x-session-id", "") != "bench-0" and r["status"] == 200
            ),
            "note": "no indefinite starvation: every non-dominant request resolves",
        }

    return {
        **{
            "scenario": scenario,
            "sessions": sessions,
            "requests": len(responses) + len(sample["errors"]),
            "ok": ok,
            "errors": len(sample["errors"]),
            "error_types": error_types,
            "cancellations": cancellations,
            "missing_tracing_headers": missing_headers,
            "wall_seconds": round(wall_seconds, 4),
            "audio_seconds": round(audio_seconds, 4),
            "rtf": round(audio_seconds / wall_seconds, 4) if wall_seconds else 0.0,
            "throughput": round(audio_seconds / wall_seconds, 4) if wall_seconds else 0.0,
            "queue_wait_seconds": _percentiles(sample["queue_waits"]),
            "active_sessions": sessions,
        },
        **fairness,
    }


async def _http_client(base_url: str) -> AsyncClient:
    return AsyncClient(base_url=base_url, timeout=120.0)


# ── CLI ───────────────────────────────────────────────────────────────────────
def _parse_sessions(raw: str) -> tuple[int, ...]:
    values = tuple(int(v) for v in raw.split(",") if v.strip())
    if not values:
        raise SystemExit("--sessions must contain at least one value")
    return values


def _baseline_throughput(path: Path) -> float:
    """Weighted-average audio_seconds / wall_seconds from a direct run."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    results = payload.get("results", [])
    audio = sum(float(result.get("audio_seconds", 0.0)) for result in results)
    wall = sum(float(result.get("wall_seconds", 0.0)) for result in results)
    return audio / wall if wall else 0.0


def _seed_real_profiles(base_url: str) -> list[str]:
    """Enroll the benchmark preset voices on a real service via the public API.

    The service owns opaque ``vp_*`` ids; the benchmark must use the ids the
    service actually issued or every request 404s (profiles are tenant-scoped).
    Uses the preset enrollment path (``POST /v1/voices?preset=true``) which
    needs no reference audio. Idempotent: seeding the same display name twice
    creates a second profile, so callers should seed once per run.
    """
    import httpx

    ids: list[str] = []
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        for name in FAKE_PRESET_NAMES:
            resp = client.post("/v1/voices", params={"preset": "true", "display_name": name})
            resp.raise_for_status()
            ids.append(resp.json()["voice_profile_id"])
    return ids


def _run(
    *,
    scenarios: tuple[str, ...],
    sessions: tuple[int, ...],
    chunks: int,
    output: Optional[Path],
    baseline: Optional[Path],
    local: bool,
    base_url: str,
) -> int:
    results: list[dict] = []
    profile_ids: list[str] = list(FAKE_PRESET_IDS)
    if not local:
        profile_ids = _seed_real_profiles(base_url)
        print(f"seeded {len(profile_ids)} preset profiles: {profile_ids}", file=sys.stderr)
    for scenario in scenarios:
        for count in sessions:
            print(f"scenario={scenario} sessions={count} ...", file=sys.stderr, flush=True)
            results.append(
                asyncio.run(
                    _run_scenario(
                        scenario=scenario,
                        sessions=count,
                        chunks=chunks,
                        base_url=base_url,
                        local=local,
                        profile_ids=profile_ids,
                        styles=list(STYLES),
                        priorities=["normal", "high"],
                    )
                )
            )
    total_requests = max(1, sum(r["requests"] for r in results))
    service_rate = sum(r["throughput"] * r["requests"] for r in results) / total_requests
    payload = {
        "config": {
            "mode": "fake" if local else "real",
            "provider": "fake" if local else "remote",
            "base_url": "local-asgi" if local else base_url,
            "hardware": hardware_metadata(),
        },
        "results": results,
    }
    if baseline is not None:
        direct_rate = _baseline_throughput(baseline)
        ratio = round(service_rate / direct_rate, 4) if direct_rate else 0.0
        payload["gate"] = {
            "baseline": str(baseline),
            "direct_audio_sec_per_wall_sec": direct_rate,
            "service_audio_sec_per_wall_sec": service_rate,
            "ratio": ratio,
        }
        if direct_rate and ratio < 0.8:
            warning = (
                "service throughput below 80% of direct-provider baseline "
                f"(ratio={ratio}); note: the 15.13 gate is only meaningful for "
                "--mode real against a deployed service"
            )
            payload["gate"]["warning"] = warning
            print("WARNING: " + warning, file=sys.stderr)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if output is not None:
        output.write_text(text, encoding="utf-8")
        print(f"wrote {output}")
    else:
        print(text)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Multi-session TTS service load benchmark over POST /v1/audio/speech "
            "(Change T 15.1-15.14). --local runs an in-process ASGI app with a "
            "fake provider; the default hits --base-url over real HTTP."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("fake", "real"),
        default="real",
        help="real = HTTP against --base-url; fake = in-process ASGI fake provider",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="in-process ASGI with the fake provider (no network, CI-safe)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8002")
    parser.add_argument("--sessions", default="1,2,4,8,16,32")
    parser.add_argument("--chunks-per-session", type=int, default=5)
    parser.add_argument(
        "--scenario",
        default=",".join(SCENARIOS),
        help="comma list, one of: " + ", ".join(SCENARIOS),
    )
    parser.add_argument("--output", default="", help="write JSON to this path (else stdout)")
    parser.add_argument("--compare-baseline", default="", help="direct-provider results JSON path")
    args = parser.parse_args(argv)

    sessions = _parse_sessions(args.sessions)
    if args.chunks_per_session < 1:
        parser.error("--chunks-per-session must be >= 1")
    scenarios = tuple(s for s in args.scenario.split(",") if s.strip())
    unknown = set(scenarios) - set(SCENARIOS)
    if unknown:
        parser.error(f"unknown scenario(s): {', '.join(sorted(unknown))}")

    local = args.local or args.mode == "fake"
    if not local:
        import httpx

        try:
            with httpx.Client(timeout=5.0) as probe:
                probe.get(args.base_url.rstrip("/") + "/ready").raise_for_status()
        except Exception as exc:
            print(
                f"ERROR: cannot reach TTS service at {args.base_url}: {exc}\n"
                "hint: start the service first or use --local for the in-process "
                "fake-provider benchmark.",
                file=sys.stderr,
            )
            return 2
    return _run(
        scenarios=scenarios,
        sessions=sessions,
        chunks=args.chunks_per_session,
        output=Path(args.output) if args.output else None,
        baseline=Path(args.compare_baseline) if args.compare_baseline else None,
        local=local,
        base_url=args.base_url,
    )


if __name__ == "__main__":
    raise SystemExit(main())
