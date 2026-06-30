"""FastAPI app — backend token broker for the LiveAvatar frontend.

The browser must NEVER see the X-API-KEY. This server is the only thing
that holds it. It exposes three endpoints the frontend can safely call:

  POST /api/session/full   -> create + start a FULL-mode session,
                              return { livekit_url, livekit_client_token, session_id }
  POST /api/session/lite   -> create + start a LITE-mode session,
                              return the same + ws_url is kept server-side
  POST /api/session/stop   -> stop a session by token (server holds tokens)

Only livekit_url + livekit_client_token cross to the browser. The
session_token (used for start/stop/keep-alive) stays here, keyed by
session_id.

Run:
    uv run uvicorn providers.liveavatar_cloud.service.server:app --port 8800
or:
    python -m providers.liveavatar_cloud.service.server
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from ..sdk.client import LiveAvatarClient, LiveAvatarError, SANDBOX_AVATAR_ID

app = FastAPI(title="LiveAvatar Token Broker")

# Allow the local frontend (Gradio/static) to call this during dev.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dev only — lock down in production
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: Optional[LiveAvatarClient] = None
# session_id -> session_token (kept server-side; never sent to browser)
_SESSIONS: dict[str, str] = {}
# session_id -> ws_url (LITE only; backend/agent uses it)
_WS_URLS: dict[str, str] = {}


def client() -> LiveAvatarClient:
    global _client
    if _client is None:
        _client = LiveAvatarClient()
    return _client


# ── Request models ──────────────────────────────────────────────────


class FullSessionRequest(BaseModel):
    avatar_id: Optional[str] = None
    context_id: Optional[str] = None
    voice_id: Optional[str] = None
    language: str = "en"
    is_sandbox: bool = True
    video_quality: str = "high"


class LiteSessionRequest(BaseModel):
    avatar_id: Optional[str] = None
    is_sandbox: bool = True


class StopRequest(BaseModel):
    session_id: str


# ── Frontend-safe responses ─────────────────────────────────────────


class SessionResponse(BaseModel):
    session_id: str
    livekit_url: str
    livekit_client_token: str
    mode: str


# ── Endpoints ───────────────────────────────────────────────────────


@app.get("/api/health")
def health() -> dict[str, Any]:
    has_key = bool(os.environ.get("LIVEAVATAR_API_KEY"))
    return {"ok": True, "api_key_loaded": has_key}


@app.post("/api/session/full", response_model=SessionResponse)
def create_full_session(req: FullSessionRequest) -> SessionResponse:
    """Create + start a FULL-mode session. Returns frontend-safe fields."""
    avatar_id = req.avatar_id or SANDBOX_AVATAR_ID
    try:
        c = client()
        body = c.build_full_token(
            avatar_id=avatar_id,
            context_id=req.context_id,
            voice_id=req.voice_id,
            language=req.language,
            is_sandbox=req.is_sandbox,
            video_quality=req.video_quality,
        )
        token = c.create_session_token(body)
        started = c.start_session(token.session_token)
    except LiveAvatarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _SESSIONS[token.session_id] = token.session_token
    return SessionResponse(
        session_id=token.session_id,
        livekit_url=started.livekit_url,
        livekit_client_token=started.livekit_client_token,
        mode="FULL",
    )


@app.post("/api/session/lite", response_model=SessionResponse)
def create_lite_session(req: LiteSessionRequest) -> SessionResponse:
    """Create + start a LITE-mode session.

    ws_url (audio channel) is kept server-side — a real LITE deployment
    runs the STT/LLM/TTS agent here and streams PCM to ws_url.
    """
    avatar_id = req.avatar_id or SANDBOX_AVATAR_ID
    try:
        c = client()
        body = c.build_lite_token(avatar_id=avatar_id, is_sandbox=req.is_sandbox)
        token = c.create_session_token(body)
        started = c.start_session(token.session_token)
    except LiveAvatarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    _SESSIONS[token.session_id] = token.session_token
    if started.ws_url:
        _WS_URLS[token.session_id] = started.ws_url
    return SessionResponse(
        session_id=token.session_id,
        livekit_url=started.livekit_url,
        livekit_client_token=started.livekit_client_token,
        mode="LITE",
    )


@app.post("/api/session/stop")
def stop_session(req: StopRequest) -> dict[str, Any]:
    """Stop a session by session_id (server resolves the token)."""
    token = _SESSIONS.pop(req.session_id, None)
    _WS_URLS.pop(req.session_id, None)
    if not token:
        raise HTTPException(status_code=404, detail="unknown session_id")
    try:
        client().stop_session(token)
    except LiveAvatarError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return {"ok": True, "stopped": req.session_id}


def main() -> None:
    import uvicorn

    port = int(os.environ.get("LIVEAVATAR_BROKER_PORT", "8800"))
    uvicorn.run(app, host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
