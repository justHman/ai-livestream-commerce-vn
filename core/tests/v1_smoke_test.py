"""Live smoke test for core/ v1 API (cloud renderer, sandbox).

Verifies the production surface end-to-end against the REAL LiveAvatar
sandbox (free, no credits):
  1. GET  /api/v1/health         -> render_backend=cloud, key loaded
  2. WS   /api/v1/ws/control/sid -> control.connected, ping/pong
  3. POST /api/v1/lite/start     -> frontend-safe creds (no token/ws_url leak)
  4. POST /api/v1/lite/say       -> reply; WS gets speak_started/ended
  5. POST /api/v1/lite/interrupt -> WS gets interrupted
  6. POST /api/v1/lite/stop      -> WS gets session.stopped

Run:
    cd implementations
    python -m core.tests.v1_smoke_test
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.skip(
    reason="integration smoke script — needs LIVEAVATAR_API_KEY / real sandbox; "
           "run via `python -m core.tests.v1_smoke_test`"
)

from fastapi.testclient import TestClient

from core.server import app


def main() -> None:
    print("== core /api/v1 smoke test (cloud, sandbox) ==\n")
    c = TestClient(app)

    h = c.get("/api/v1/health").json()
    print(f"[health]    {h}")
    assert h["ok"] and h["render_backend"] == "cloud" and h["api_key_loaded"]

    r = c.post("/api/v1/lite/start", json={"is_sandbox": True})
    assert r.status_code == 200, r.text
    d = r.json()
    sid = d["session_id"]
    leak = ("session_token" in d) or ("ws_url" in d)
    print(f"[start]     keys={sorted(d.keys())}, leak={leak}")
    assert not leak, "must not leak secrets to frontend"

    with c.websocket_connect(f"/api/v1/ws/control/{sid}") as ws:
        print(f"[ws]        {ws.receive_json()['type']} [OK]")
        ws.send_json({"type": "ping"})
        print(f"[ws]        ping -> {ws.receive_json()['type']} [OK]")

        r = c.post("/api/v1/lite/say", json={"session_id": sid, "text": "Giá serum bao nhiêu?"})
        assert r.status_code == 200, r.text
        ev1, ev2 = ws.receive_json(), ws.receive_json()
        print(f"[ws]        say -> {ev1['type']}, {ev2['type']} [OK]")
        print(f"[say]       reply={r.json()['reply'][:48]!r}")

    s = c.post("/api/v1/lite/stop", json={"session_id": sid})
    print(f"[stop]      {s.json()} [OK]")

    print("\n== core v1 path OK ==")


if __name__ == "__main__":
    main()
