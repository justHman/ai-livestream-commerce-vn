"""LiveAvatar API client — backend-only.

Wraps the LiveAvatar REST API (https://api.liveavatar.com) for the
Vietnamese live-commerce host use case. This module NEVER runs in the
browser: it holds the X-API-KEY secret and mints short-lived session
tokens / client tokens that are safe to hand to the frontend.

Auth model (do not mix these up):
  - X-API-KEY            -> backend only. Creates session tokens, contexts,
                            secrets, LLM configs, embeds, voices.
  - Bearer session_token -> backend. Starts/stops/keep-alives a session.
  - livekit_client_token -> frontend. Joins the LiveKit room (video).
  - ws_url               -> backend/agent (LITE only). Streams PCM audio.

Reference: liveavatar-integrate skill, full-mode-guide.md / lite-mode-guide.md.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

# ── Load .env from the implementations/ folder (one level up) ───────────
_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _load_env(path: Path = _ENV_PATH) -> None:
    """Minimal .env loader (no python-dotenv dependency).

    Only sets keys that are not already in the environment, so real
    environment variables always win over the file.
    """
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


_load_env()

DEFAULT_API_BASE = os.environ.get("LIVEAVATAR_API_BASE", "https://api.liveavatar.com")
# Sandbox avatar for free ~1-min sessions (no credits). Session-mode sandbox ID.
SANDBOX_AVATAR_ID = os.environ.get(
    "LIVEAVATAR_SANDBOX_AVATAR_ID", "dd73ea75-1218-4ef3-92ce-606d5f7fbc0a"
)


class LiveAvatarError(RuntimeError):
    """Raised when the LiveAvatar API returns a non-success response."""


@dataclass
class SessionToken:
    """Result of creating a session token."""

    session_id: str
    session_token: str  # secret-ish: backend uses it; never log in full


@dataclass
class StartedSession:
    """Result of starting a session.

    For FULL mode, `ws_url` is None. For LITE mode it carries the
    WebSocket URL the backend streams PCM audio to.
    """

    livekit_url: str
    livekit_client_token: str  # SAFE for frontend
    ws_url: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)


class LiveAvatarClient:
    """Thin synchronous client over the LiveAvatar REST API.

    Parameters
    ----------
    api_key : str or None
        LiveAvatar X-API-KEY. Falls back to LIVEAVATAR_API_KEY env var.
    api_base : str
        API base URL.
    timeout : float
        Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base: str = DEFAULT_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self.api_key = api_key or os.environ.get("LIVEAVATAR_API_KEY")
        if not self.api_key:
            raise LiveAvatarError(
                "No API key. Set LIVEAVATAR_API_KEY in implementations/.env or pass api_key=..."
            )
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Low-level request helpers
    # ------------------------------------------------------------------

    def _headers_key(self) -> dict[str, str]:
        return {"X-API-KEY": self.api_key, "Content-Type": "application/json"}

    @staticmethod
    def _headers_bearer(session_token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {session_token}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        json_body: Optional[dict] = None,
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        resp = requests.request(method, url, headers=headers, json=json_body, timeout=self.timeout)
        try:
            payload = resp.json()
        except ValueError:
            payload = {"raw_text": resp.text}

        if resp.status_code >= 400 or (
            isinstance(payload, dict) and payload.get("code") not in (None, 1000)
        ):
            raise LiveAvatarError(f"{method} {path} -> HTTP {resp.status_code}: {payload}")
        return payload

    # ------------------------------------------------------------------
    # Discovery (X-API-KEY)
    # ------------------------------------------------------------------

    def get_credits(self) -> float:
        """Return remaining credits. Free tier starts at 10.

        FULL/Embed = 2 credits/min, LITE = 1 credit/min. A session won't
        start without enough credits for at least one minute.
        """
        payload = self._request("GET", "/v1/users/credits", headers=self._headers_key())
        return float(payload["data"]["credits_left"])

    def list_avatars(self) -> list[dict[str, Any]]:
        """Return all avatars on this account (paginated, flattened)."""
        return self._paginate("/v1/avatars")

    def list_voices(self) -> list[dict[str, Any]]:
        """Return all available voices (paginated, flattened)."""
        return self._paginate("/v1/voices")

    def _paginate(self, path: str) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        page = 1
        while True:
            payload = self._request(
                "GET",
                f"{path}?page={page}&page_size=20",
                headers=self._headers_key(),
            )
            data = payload.get("data", {})
            results.extend(data.get("results", []))
            if not data.get("next"):
                break
            page += 1
        return results

    # ------------------------------------------------------------------
    # Contexts (X-API-KEY) — the avatar's brain in FULL mode
    # ------------------------------------------------------------------

    def create_context(
        self,
        name: str,
        prompt: str,
        opening_text: str,
        links: Optional[list[dict[str, str]]] = None,
    ) -> str:
        """Create a reusable context. Returns the context_id.

        Without a context, a FULL-mode avatar streams video but stays
        SILENT (no error). Always create one.
        """
        body: dict[str, Any] = {
            "name": name,
            "prompt": prompt,
            "opening_text": opening_text,
        }
        if links:
            body["links"] = links
        payload = self._request("POST", "/v1/contexts", headers=self._headers_key(), json_body=body)
        return payload["data"]["id"]

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def create_session_token(self, body: dict[str, Any]) -> SessionToken:
        """Create a session token (BACKEND, X-API-KEY).

        `body` is the raw session-token request (mode, avatar_id,
        avatar_persona, etc.). Use the build_*_token helpers below.
        """
        payload = self._request(
            "POST",
            "/v1/sessions/token",
            headers=self._headers_key(),
            json_body=body,
        )
        data = payload["data"]
        return SessionToken(
            session_id=data["session_id"],
            session_token=data["session_token"],
        )

    def start_session(self, session_token: str) -> StartedSession:
        """Start a session (BACKEND, Bearer <session_token>).

        IMPORTANT: use the session token here, NOT the API key. Using
        X-API-KEY on this call is the #1 auth mistake.
        """
        payload = self._request(
            "POST",
            "/v1/sessions/start",
            headers=self._headers_bearer(session_token),
        )
        data = payload["data"]
        return StartedSession(
            livekit_url=data["livekit_url"],
            livekit_client_token=data["livekit_client_token"],
            ws_url=data.get("ws_url"),
            raw=data,
        )

    def keep_alive(self, session_token: str) -> None:
        """Keep a session alive (call every 2-3 min; 5-min timeout)."""
        self._request(
            "POST",
            "/v1/sessions/keep-alive",
            headers=self._headers_bearer(session_token),
        )

    def stop_session(self, session_token: str) -> None:
        """Stop a session and free resources / stop credit usage."""
        self._request(
            "POST",
            "/v1/sessions/stop",
            headers=self._headers_bearer(session_token),
        )

    # ------------------------------------------------------------------
    # Token builders — encode the mode differences in one place
    # ------------------------------------------------------------------

    @staticmethod
    def build_full_token(
        avatar_id: str,
        context_id: Optional[str] = None,
        voice_id: Optional[str] = None,
        language: str = "en",
        llm_configuration_id: Optional[str] = None,
        interactivity_type: str = "CONVERSATIONAL",
        is_sandbox: bool = False,
        video_quality: str = "high",
    ) -> dict[str, Any]:
        """Build a FULL-mode session-token body.

        FULL = LiveAvatar runs ASR + LLM + TTS + video. 2 credits/min.
        """
        persona: dict[str, Any] = {"language": language}
        if voice_id:
            persona["voice_id"] = voice_id
        if context_id:
            persona["context_id"] = context_id

        body: dict[str, Any] = {
            "mode": "FULL",
            "avatar_id": avatar_id,
            "avatar_persona": persona,
            "interactivity_type": interactivity_type,
            "video_quality": video_quality,
        }
        if llm_configuration_id:
            body["llm_configuration_id"] = llm_configuration_id
        if is_sandbox:
            body["is_sandbox"] = True
        return body

    @staticmethod
    def build_lite_token(
        avatar_id: str,
        is_sandbox: bool = False,
    ) -> dict[str, Any]:
        """Build a LITE-mode session-token body.

        LITE = you bring STT + LLM + TTS; LiveAvatar only renders video
        from PCM audio you stream over ws_url. 1 credit/min.
        """
        body: dict[str, Any] = {"mode": "LITE", "avatar_id": avatar_id}
        if is_sandbox:
            body["is_sandbox"] = True
        return body
