"""P2 Colab smoke runner for the AI livestream backend.

Runs against a live backend (typically the ngrok-backed Colab server) and checks
that the P1/P2 demo surface works end-to-end:

  health/ready -> engines -> lite/start -> lite/attach -> lite/chat burst ->
  mock/frame -> mock/video MJPEG sample -> lite/stop

The script is intentionally HTTP-only and dependency-light (stdlib + httpx) so it
can run inside Colab, locally against ngrok, or in CI with a mock app already
running. It does NOT load models itself and does NOT require a local GPU.
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from core.debug.mock_data import MOCK_PRODUCTS, MOCK_VIEWER_MSGS


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    elapsed_ms: float = 0.0


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _json_headers(token: str) -> dict[str, str]:
    return {**_headers(token), "Content-Type": "application/json"}


def _print_check(check: Check) -> None:
    mark = "PASS" if check.ok else "FAIL"
    print(f"{mark:4} {check.name:<28} {check.elapsed_ms:8.1f} ms  {check.detail}")


def _request_first(
    client: httpx.Client,
    method: str,
    base_url: str,
    path: str,
    *,
    token: str = "",
    json_body: Optional[dict[str, Any]] = None,
    timeout: float = 20.0,
) -> tuple[httpx.Response, str]:
    """Try /api/v1 then root path, returning the first non-404 response."""
    last: Optional[httpx.Response] = None
    for prefix in ("/api/v1", ""):
        headers = _json_headers(token) if json_body is not None else _headers(token)
        response = client.request(
            method,
            f"{base_url}{prefix}{path}",
            json=json_body,
            headers=headers,
            timeout=timeout,
        )
        last = response
        if response.status_code != 404:
            return response, prefix
    assert last is not None
    return last, ""


def _read_mjpeg_parts(
    client: httpx.Client,
    url: str,
    *,
    max_parts: int,
    max_seconds: float,
) -> tuple[int, int, float]:
    """Read a small MJPEG sample and count multipart boundaries."""
    start = _now_ms()
    deadline = time.perf_counter() + max_seconds
    chunks: list[bytes] = []
    with client.stream("GET", url, timeout=max_seconds + 5.0) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            if chunk:
                chunks.append(chunk)
            raw = b"".join(chunks)
            parts = raw.count(b"--mockmjpegboundary")
            if parts >= max_parts or time.perf_counter() >= deadline:
                return parts, len(raw), _now_ms() - start
    raw = b"".join(chunks)
    return raw.count(b"--mockmjpegboundary"), len(raw), _now_ms() - start


def run_smoke(
    base_url: str,
    *,
    token: str = "",
    chat_count: int = 10,
    mjpeg_parts: int = 10,
    mjpeg_seconds: float = 3.0,
) -> tuple[list[Check], dict[str, Any]]:
    """Run the P2 smoke flow and return checks plus raw metrics."""
    base_url = base_url.rstrip("/")
    checks: list[Check] = []
    metrics: dict[str, Any] = {"base_url": base_url}
    sid: Optional[str] = None
    api_prefix = "/api/v1"

    with httpx.Client(timeout=30.0) as client:
        # readiness
        t0 = _now_ms()
        ready, api_prefix = _request_first(client, "GET", base_url, "/health/ready", token=token)
        elapsed = _now_ms() - t0
        ready_ok = ready.status_code == 200 and ready.json().get("ok") is not False
        checks.append(Check("health.ready", ready_ok, ready.text[:220], elapsed))
        metrics["health_ready"] = (
            ready.json()
            if ready.headers.get("content-type", "").startswith("application/json")
            else ready.text
        )

        # engines + preset count
        t0 = _now_ms()
        engines, _ = _request_first(client, "GET", base_url, "/engines", token=token)
        elapsed = _now_ms() - t0
        engines_json = engines.json() if engines.status_code == 200 else {}
        preset_count = len(engines_json.get("available_tts_presets", []))
        checks.append(
            Check(
                "engines.tts_presets",
                engines.status_code == 200 and preset_count >= 6,
                f"status={engines.status_code} tts_presets={preset_count}",
                elapsed,
            )
        )
        metrics["engines"] = engines_json

        # start
        t0 = _now_ms()
        start, api_prefix = _request_first(
            client,
            "POST",
            base_url,
            "/lite/start",
            token=token,
            json_body={"is_sandbox": True},
        )
        elapsed = _now_ms() - t0
        start_json = start.json() if start.status_code == 200 else {}
        sid = start_json.get("session_id")
        checks.append(
            Check("lite.start", start.status_code == 200 and bool(sid), start.text[:220], elapsed)
        )
        metrics["session_id"] = sid
        metrics["mode"] = start_json.get("mode")

        if not sid:
            return checks, metrics

        try:
            # attach
            t0 = _now_ms()
            attach, _ = _request_first(
                client,
                "POST",
                base_url,
                "/lite/attach",
                token=token,
                json_body={"session_id": sid, "products": MOCK_PRODUCTS},
            )
            elapsed = _now_ms() - t0
            checks.append(
                Check("lite.attach", attach.status_code == 200, attach.text[:220], elapsed)
            )

            # chat burst
            latencies: list[float] = []
            accepted = 0
            for i in range(chat_count):
                msg = MOCK_VIEWER_MSGS[i % len(MOCK_VIEWER_MSGS)]
                t0 = _now_ms()
                chat, _ = _request_first(
                    client,
                    "POST",
                    base_url,
                    "/lite/chat",
                    token=token,
                    json_body={
                        "session_id": sid,
                        "text": msg,
                        "author": f"p2_viewer_{i:03d}",
                        "ts": time.time(),
                    },
                    timeout=10.0,
                )
                latencies.append(_now_ms() - t0)
                if chat.status_code == 202:
                    accepted += 1
            max_chat_ms = max(latencies) if latencies else 0.0
            checks.append(
                Check(
                    "lite.chat_burst",
                    accepted == chat_count and max_chat_ms < 1000.0,
                    f"accepted={accepted}/{chat_count} max={max_chat_ms:.1f}ms",
                    sum(latencies),
                )
            )
            metrics["chat"] = {
                "accepted": accepted,
                "count": chat_count,
                "latencies_ms": latencies,
                "max_ms": max_chat_ms,
            }

            # Give coordinator a short window to process comments.
            time.sleep(1.5)

            # frame snapshot
            t0 = _now_ms()
            frame, _ = _request_first(
                client, "GET", base_url, f"/mock/frame/{sid}.png", token=token
            )
            elapsed = _now_ms() - t0
            frame_size = len(frame.content) if frame.status_code == 200 else 0
            checks.append(
                Check(
                    "mock.frame",
                    frame.status_code == 200 and frame_size > 5 * 1024,
                    f"status={frame.status_code} bytes={frame_size}",
                    elapsed,
                )
            )
            metrics["frame_bytes"] = frame_size

            # MJPEG sample
            mjpeg_url = f"{base_url}{api_prefix}/mock/video/{sid}.mjpeg"
            try:
                parts, byte_count, elapsed = _read_mjpeg_parts(
                    client,
                    mjpeg_url,
                    max_parts=mjpeg_parts,
                    max_seconds=mjpeg_seconds,
                )
                checks.append(
                    Check(
                        "mock.mjpeg",
                        parts >= mjpeg_parts,
                        f"parts={parts} bytes={byte_count} url={mjpeg_url}",
                        elapsed,
                    )
                )
                metrics["mjpeg"] = {
                    "parts": parts,
                    "bytes": byte_count,
                    "elapsed_ms": elapsed,
                    "url": mjpeg_url,
                }
            except Exception as exc:
                checks.append(Check("mock.mjpeg", False, f"{type(exc).__name__}: {exc}"))
                metrics["mjpeg"] = {"error": f"{type(exc).__name__}: {exc}", "url": mjpeg_url}
        finally:
            t0 = _now_ms()
            stop, _ = _request_first(
                client,
                "POST",
                base_url,
                "/lite/stop",
                token=token,
                json_body={"session_id": sid},
                timeout=10.0,
            )
            checks.append(
                Check("lite.stop", stop.status_code == 200, stop.text[:220], _now_ms() - t0)
            )

    return checks, metrics


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run P2 smoke checks against a live backend")
    parser.add_argument(
        "--base-url",
        required=True,
        help="Backend base URL, e.g. http://127.0.0.1:8000 or ngrok URL",
    )
    parser.add_argument("--token", default="", help="Viewer/admin API token if APP_ENV is not dev")
    parser.add_argument("--chat-count", type=int, default=10)
    parser.add_argument("--mjpeg-parts", type=int, default=10)
    parser.add_argument("--mjpeg-seconds", type=float, default=3.0)
    parser.add_argument(
        "--json", action="store_true", help="Print raw JSON metrics after the table"
    )
    args = parser.parse_args(argv)

    checks, metrics = run_smoke(
        args.base_url,
        token=args.token,
        chat_count=args.chat_count,
        mjpeg_parts=args.mjpeg_parts,
        mjpeg_seconds=args.mjpeg_seconds,
    )

    print("P2 Colab smoke report")
    print("=" * 72)
    for check in checks:
        _print_check(check)
    print("=" * 72)
    ok = all(c.ok for c in checks)
    print("RESULT", "PASS" if ok else "FAIL")
    if args.json:
        print(
            json.dumps(
                {"ok": ok, "checks": [c.__dict__ for c in checks], "metrics": metrics},
                ensure_ascii=False,
                indent=2,
            )
        )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
