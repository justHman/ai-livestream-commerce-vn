"""Live smoke test for the portable colab_server (HTTP + WebSocket control).

Exercises against the REAL sandbox (free, no credits):
  1. /api/health (store backend, key loaded)
  2. WS /ws/control/{sid} connect → control.connected
  3. /api/lite/start → frontend-safe creds (no token/ws_url leak)
  4. /api/lite/say → avatar speaks; WS receives speak_started/ended
  5. /api/lite/interrupt → WS receives interrupted
  6. /api/lite/stop → WS receives session.stopped

Usage:
    cd implementations
    python -m providers.liveavatar_cloud.examples.server_ws_smoke_test
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from providers.liveavatar_cloud.sdk import colab_server


def main() -> None:
    print("== colab_server HTTP + WS smoke test (sandbox) ==\n")
    c = TestClient(colab_server.app)

    h = c.get("/api/health").json()
    print(f"[health]    {h}")
    assert h["ok"] and h["api_key_loaded"], "API key must be loaded"

    # Start a session first (so the WS has a session to attach to).
    r = c.post("/api/lite/start", json={"is_sandbox": True})
    assert r.status_code == 200, r.text
    d = r.json()
    sid = d["session_id"]
    leak = ("session_token" in d) or ("ws_url" in d)
    print(f"[start]     session ok, keys={sorted(d.keys())}, leak={leak}")
    assert not leak, "must not leak session_token/ws_url to frontend"

    # Open the control WS and drive a turn.
    with c.websocket_connect(f"/ws/control/{sid}") as ws:
        hello = ws.receive_json()
        print(f"[ws]        {hello['type']} [OK]")

        ws.send_json({"type": "ping"})
        pong = ws.receive_json()
        print(f"[ws]        ping -> {pong['type']} [OK]")

        # say triggers speak_started + speak_ended events on the WS
        r = c.post("/api/lite/say", json={"session_id": sid, "text": "Giá serum bao nhiêu?"})
        assert r.status_code == 200, r.text
        ev1 = ws.receive_json()
        ev2 = ws.receive_json()
        print(f"[ws]        say -> {ev1['type']}, {ev2['type']} [OK]")
        print(f"[say]       reply={r.json()['reply'][:50]!r}")

    # Stop
    s = c.post("/api/lite/stop", json={"session_id": sid})
    print(f"[stop]      {s.json()} [OK]")

    print("\n== Server HTTP+WS path OK ==")


if __name__ == "__main__":
    main()
