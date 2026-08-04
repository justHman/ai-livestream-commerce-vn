#!/usr/bin/env python3
"""Workbench black-box smoke test — canonical REST+WS session flow.

External CLI configured by base URL/tokens. Never imports backend internals
and never mutates processes. Stdlib + httpx (declared root tool deps) only.

Flow: health/ready -> engines -> start session -> attach -> chat/ingest ->
platform WS -> interrupt -> stop, with cleanup in finally.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    elapsed_ms: float = 0.0


@dataclass
class SmokeConfig:
    base_url: str
    viewer_token: str = ""
    admin_token: str = ""
    timeout_sec: float = 30.0
    chat_count: int = 3


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _headers(token: str, json_body: bool = False) -> dict[str, str]:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def _print_check(check: Check) -> None:
    mark = "PASS" if check.ok else "FAIL"
    print(f"{mark:4} {check.name:<28} {check.elapsed_ms:8.1f} ms  {check.detail}")


async def smoke(cfg: SmokeConfig) -> list[Check]:
    checks: list[Check] = []
    base = cfg.base_url.rstrip("/")
    session_id: Optional[str] = None

    async with httpx.AsyncClient(timeout=cfg.timeout_sec) as client:
        # ── health/ready ──
        t0 = _now_ms()
        ready = await client.get(f"{base}/api/v1/health/ready", headers=_headers(cfg.viewer_token))
        checks.append(Check(
            "health.ready",
            ready.status_code == 200 and ready.json().get("ok") is not False,
            ready.text[:200],
            _now_ms() - t0,
        ))

        # ── engines ──
        t0 = _now_ms()
        engines = await client.get(f"{base}/api/v1/engines", headers=_headers(cfg.admin_token))
        engines_json = engines.json() if engines.status_code == 200 else {}
        checks.append(Check(
            "engines",
            engines.status_code == 200,
            f"status={engines.status_code}",
            _now_ms() - t0,
        ))

        # ── start session (canonical /api/v1/sessions) ──
        t0 = _now_ms()
        start = await client.post(
            f"{base}/api/v1/sessions",
            headers=_headers(cfg.viewer_token, json_body=True),
            json={"avatar_id": None, "is_sandbox": True},
        )
        start_json = start.json() if start.status_code == 200 else {}
        session_id = start_json.get("session_id")
        checks.append(Check(
            "sessions.start",
            start.status_code == 200 and bool(session_id),
            start.text[:200],
            _now_ms() - t0,
        ))
        if not session_id:
            return checks

        try:
            # ── attach (canonical path-style) ──
            t0 = _now_ms()
            products = [{
                "id": "P004", "name": "Áo hoodie", "description": "Áo hoodie trơn", "price": 350000,
                "original_price": 500000, "promotion": "Giảm 30%", "colors": ["trắng kem"], "sizes": ["M", "L"],
                "material": "nỉ cotton", "shipping": "Freeship", "warranty": "Đổi trả 7 ngày", "in_stock": True,
                "stock_total": 120, "ref_image": "", "features": ["mũ", "logo"],
            }]
            attach = await client.post(
                f"{base}/api/v1/sessions/{urllib.parse.quote(session_id)}/attach",
                headers=_headers(cfg.viewer_token, json_body=True),
                json={
                    "shop_profile": {"shop_name": "Shop Demo", "host_name": "Chị Lan", "address": "TP.HCM", "phone": "0909.123.456", "selling_style": "Nhiệt tình."},
                    "products": products,
                },
            )
            checks.append(Check(
                "sessions.attach",
                attach.status_code == 200,
                attach.text[:200],
                _now_ms() - t0,
            ))

            # ── chat burst (canonical path-style, 202) ──
            accepted = 0
            for index in range(cfg.chat_count):
                t0 = _now_ms()
                chat = await client.post(
                    f"{base}/api/v1/sessions/{urllib.parse.quote(session_id)}/chat",
                    headers=_headers(cfg.viewer_token, json_body=True),
                    json={"text": f"Giá sản phẩm bao nhiêu? {index}", "author": f"smoke_{index}", "ts": time.time()},
                )
                if chat.status_code == 202:
                    accepted += 1
                checks.append(Check(
                    "sessions.chat",
                    chat.status_code == 202,
                    chat.text[:120],
                    _now_ms() - t0,
                ))
            checks.append(Check(
                "sessions.chat_burst",
                accepted == cfg.chat_count,
                f"accepted={accepted}/{cfg.chat_count}",
                0.0,
            ))

            # ── platform WS (canonical /ws/platform/{session}) ──
            t0 = _now_ms()
            ws_url = f"{base.replace('http', 'ws', 1)}/api/v1/ws/platform/{urllib.parse.quote(session_id)}"
            if cfg.viewer_token:
                ws_url += f"?token={urllib.parse.quote(cfg.viewer_token)}"
            ws_ok = False
            ws_detail = "ws not tested (httpx-ws unavailable)"
            try:
                import websockets  # optional — skip when unavailable

                async with websockets.connect(ws_url, timeout=10.0) as ws:
                    await ws.send(json.dumps({"text": "Chào shop, giá bao nhiêu?", "author": "smoke_ws"}))
                    reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                    ws_ok = reply.get("type") in ("platform.accepted", "platform.stored")
                    ws_detail = f"type={reply.get('type')}"
            except ImportError:
                ws_detail = "websockets module not installed; skipped"
            except Exception as exc:  # noqa: BLE001
                ws_detail = f"{type(exc).__name__}: {exc}"
            checks.append(Check("platform.ws", ws_ok, ws_detail, _now_ms() - t0))

            # ── interrupt ──
            t0 = _now_ms()
            intr = await client.post(
                f"{base}/api/v1/sessions/{urllib.parse.quote(session_id)}/interrupt",
                headers=_headers(cfg.viewer_token),
            )
            checks.append(Check(
                "sessions.interrupt",
                intr.status_code == 200,
                intr.text[:120],
                _now_ms() - t0,
            ))
        finally:
            # ── stop (always) ──
            t0 = _now_ms()
            stop = await client.post(
                f"{base}/api/v1/sessions/{urllib.parse.quote(session_id)}/stop",
                headers=_headers(cfg.viewer_token),
            )
            checks.append(Check(
                "sessions.stop",
                stop.status_code == 200,
                stop.text[:120],
                _now_ms() - t0,
            ))

    return checks


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Workbench black-box smoke test")
    parser.add_argument("--base-url", required=True, help="Backend base URL e.g. http://127.0.0.1:8800")
    parser.add_argument("--viewer-token", default="", help="Viewer token (BACKEND_API_TOKEN)")
    parser.add_argument("--admin-token", default="", help="Admin token (ADMIN_API_TOKEN)")
    parser.add_argument("--chat-count", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args(argv)

    checks = asyncio.run(smoke(SmokeConfig(
        base_url=args.base_url,
        viewer_token=args.viewer_token,
        admin_token=args.admin_token,
        timeout_sec=args.timeout,
        chat_count=args.chat_count,
    )))

    print("Workbench smoke report")
    print("=" * 72)
    for check in checks:
        _print_check(check)
    print("=" * 72)
    ok = all(c.ok for c in checks)
    print("RESULT", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())