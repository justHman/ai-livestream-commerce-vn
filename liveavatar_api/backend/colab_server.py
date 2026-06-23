"""Public LITE backend for Colab + ngrok (portable to AWS).

Two planes, kept separate (this is the architecture decision):

  CONTROL plane (this server, JSON + WebSocket):
    - HTTP: session lifecycle + "say" commands
    - WS:   /ws/control/{session_id} — pushes events to the frontend
            (speak_started/ended, errors) and receives commands
            (interrupt). Two-way, low-latency.
  MEDIA plane (NOT this server):
    - Avatar VIDEO goes LiveAvatar-cloud -> LiveKit -> browser directly.
      The browser renders it from livekit_url + livekit_client_token.
      No frames ever transit this backend.

Portability (Colab now, AWS later — same code):
  - Config from env (config.AppConfig): SESSION_STORE, REDIS_URL, CORS_ORIGINS, PORT.
  - SessionStore abstraction: InMemory (Colab) | Redis (AWS) — switch via env.
  - LLM/TTS injected via configure() — Colab uses local models, AWS points
    at a shared vLLM/TTS endpoint. Same interface.

HTTP API:
  GET  /api/health
  POST /api/lite/start  {avatar_id?, is_sandbox?}        -> {session_id, livekit_url, livekit_client_token}
  POST /api/lite/say    {session_id, text}               -> {ok, reply}     (avatar speaks via media plane)
  POST /api/lite/interrupt {session_id}                  -> {ok}            (barge-in; gated by Director later)
  POST /api/lite/stop   {session_id}                     -> {ok}
WS:
  /ws/control/{session_id}  <- server pushes events; -> client sends {type:"interrupt"} etc.

Run:
    uv run uvicorn liveavatar_api.backend.colab_server:app --port 8800
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .client import LiveAvatarClient, LiveAvatarError, SANDBOX_AVATAR_ID
from .config import AppConfig
from .conversation import LiteConversation, echo_llm, tone_tts

CONFIG = AppConfig.from_env()

app = FastAPI(title="LiveAvatar LITE Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=CONFIG.cors_list(),
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pluggable model backends. colab_deploy.py overrides these before serving.
LLM_FN = echo_llm
TTS_FN = tone_tts

_client: Optional[LiveAvatarClient] = None
_STORE = CONFIG.build_store()
# Live conversation objects are NOT serializable → kept in-process, keyed by
# session_id. Cross-instance metadata lives in _STORE; the live WS/agent object
# stays on the instance that owns the session (AWS: sticky LB guarantees this).
_CONVOS: dict[str, LiteConversation] = {}


def client() -> LiveAvatarClient:
    global _client
    if _client is None:
        _client = LiveAvatarClient()
    return _client


def configure(llm_fn=None, tts_fn=None) -> None:
    """Inject real LLM/TTS callables (called from the Colab launcher)."""
    global LLM_FN, TTS_FN
    if llm_fn is not None:
        LLM_FN = llm_fn
    if tts_fn is not None:
        TTS_FN = tts_fn


# ── WebSocket control-plane connection manager ──────────────────────────


class ControlHub:
    """Tracks one WS connection per session for server→client events."""

    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[session_id] = ws

    def disconnect(self, session_id: str) -> None:
        self._conns.pop(session_id, None)

    async def emit(self, session_id: str, event: dict) -> None:
        ws = self._conns.get(session_id)
        if ws is not None:
            try:
                await ws.send_json(event)
            except Exception:
                self.disconnect(session_id)


HUB = ControlHub()


# ── Request models ──────────────────────────────────────────────────


class StartReq(BaseModel):
    avatar_id: Optional[str] = None
    is_sandbox: bool = True


class SayReq(BaseModel):
    session_id: str
    text: str


class SessionReq(BaseModel):
    session_id: str


# ── HTTP endpoints ──────────────────────────────────────────────────


@app.get("/api/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "api_key_loaded": CONFIG.api_key_present,
        "store_backend": CONFIG.store_backend,
        "active_sessions": len(_CONVOS),
    }


@app.post("/api/lite/start")
async def start(req: StartReq) -> dict[str, Any]:
    try:
        convo = LiteConversation(
            client=client(),
            llm=LLM_FN,
            tts=TTS_FN,
            avatar_id=req.avatar_id or SANDBOX_AVATAR_ID,
            is_sandbox=req.is_sandbox,
        )
        # convo.start() is blocking (sync WS connect) → run off the event loop
        front = await asyncio.to_thread(convo.start)
    except LiveAvatarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    sid = front["session_id"]
    _CONVOS[sid] = convo
    await _STORE.set(sid, {"status": "active", "mode": "LITE"})
    return front  # only frontend-safe fields


@app.post("/api/lite/say")
async def say(req: SayReq) -> dict[str, Any]:
    convo = _CONVOS.get(req.session_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="unknown session_id")

    await HUB.emit(req.session_id, {"type": "avatar.speak_started", "text": req.text})
    reply = await asyncio.to_thread(convo.turn, req.text)
    await HUB.emit(req.session_id, {"type": "avatar.speak_ended", "reply": reply})
    return {"ok": True, "reply": reply}


@app.post("/api/lite/interrupt")
async def interrupt(req: SessionReq) -> dict[str, Any]:
    """Barge-in: stop the avatar mid-utterance.

    The Director (later) decides WHEN this fires (priority-gated); here it's
    the raw mechanism.
    """
    convo = _CONVOS.get(req.session_id)
    if convo is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if convo.agent is not None:
        convo.agent.interrupt()
    await HUB.emit(req.session_id, {"type": "avatar.interrupted"})
    return {"ok": True}


@app.post("/api/lite/stop")
async def stop(req: SessionReq) -> dict[str, Any]:
    convo = _CONVOS.pop(req.session_id, None)
    if convo is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await asyncio.to_thread(convo.stop)
    await _STORE.delete(req.session_id)
    await HUB.emit(req.session_id, {"type": "session.stopped"})
    return {"ok": True, "stopped": req.session_id}


# ── WebSocket control plane ─────────────────────────────────────────


@app.websocket("/ws/control/{session_id}")
async def ws_control(ws: WebSocket, session_id: str) -> None:
    """Two-way control channel for one session.

    Server → client: avatar.speak_started/ended, interrupted, session.stopped.
    Client → server: {"type": "interrupt"} (barge-in), {"type": "ping"}.
    """
    await HUB.connect(session_id, ws)
    await ws.send_json({"type": "control.connected", "session_id": session_id})
    try:
        while True:
            msg = await ws.receive_json()
            mtype = msg.get("type")
            if mtype == "interrupt":
                convo = _CONVOS.get(session_id)
                if convo is not None and convo.agent is not None:
                    convo.agent.interrupt()
                    await HUB.emit(session_id, {"type": "avatar.interrupted"})
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        HUB.disconnect(session_id)


def main() -> None:
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=CONFIG.port)


if __name__ == "__main__":
    main()
