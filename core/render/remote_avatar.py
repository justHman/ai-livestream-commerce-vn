"""RemoteAvatarBackend — StreamingAvatarBackend over HTTP (Task 14).

Calls a remote avatar microservice:

  POST {base}/sessions/start
  POST {base}/sessions/{id}/audio
  POST {base}/sessions/{id}/interrupt
  POST {base}/sessions/{id}/stop

Offline tests inject ``httpx.Client(transport=MockTransport(...))``.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

import httpx

from .base import StartOptions, StartResult, StreamingAvatarBackend
from .windows import AudioWindow, VideoWindow


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _join_url(base_url: str, path: str) -> str:
    base = _strip_trailing_slash(base_url) + "/"
    return urljoin(base, path.lstrip("/"))


def _raise_http(resp: httpx.Response, action: str) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = (resp.text or "")[:300]
        raise RuntimeError(
            f"remote_avatar {action} failed: HTTP {resp.status_code} {detail}"
        ) from exc


class RemoteAvatarBackend(StreamingAvatarBackend):
    """HTTP client for a remote avatar render service."""

    name = "remote_avatar"

    def __init__(
        self,
        base_url: str = "",
        *,
        timeout: float = 60.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        base = (base_url or os.environ.get("AVATAR_BASE_URL", "") or "").strip()
        if not base:
            raise ValueError(
                "remote_avatar needs base_url or env AVATAR_BASE_URL"
            )
        self._base_url = _strip_trailing_slash(base)
        self._timeout = float(timeout)
        self._client = http_client
        self._sessions: set[str] = set()

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def start(self, opts: StartOptions) -> StartResult:
        client = self._get_client()
        url = _join_url(self._base_url, "/sessions/start")
        body: dict[str, Any] = {
            "avatar_id": opts.avatar_id,
            "is_sandbox": opts.is_sandbox,
        }
        body.update(opts.extra or {})
        try:
            resp = client.post(url, json=body)
        except httpx.RequestError as exc:
            raise RuntimeError(f"remote_avatar start request failed: {exc}") from exc
        _raise_http(resp, "start")
        data = resp.json() if resp.content else {}
        session_id = str(data.get("session_id") or "")
        if not session_id:
            raise RuntimeError("remote_avatar start: missing session_id in response")
        self._sessions.add(session_id)
        return StartResult(
            session_id=session_id,
            livekit_url=str(data.get("livekit_url") or ""),
            livekit_client_token=str(data.get("livekit_client_token") or ""),
            mode=str(data.get("mode") or "REMOTE"),
        )

    def stream_audio(
        self,
        session_id: str,
        audio_window: AudioWindow,
    ) -> Iterator[VideoWindow]:
        if session_id not in self._sessions:
            # Allow remote-owned sessions that were not started via this process
            # only when caller already knows the id — still require membership
            # for local bookkeeping consistency with mock backend.
            raise KeyError(session_id)
        client = self._get_client()
        url = _join_url(self._base_url, f"/sessions/{session_id}/audio")
        body: dict[str, Any] = {
            "session_id": session_id,
            "utterance_id": audio_window.utterance_id,
            "seq": audio_window.seq,
            "sample_rate": audio_window.sample_rate,
            "duration_ms": audio_window.duration_ms,
            "is_final": audio_window.is_final,
            "text_span": audio_window.text_span,
        }
        if audio_window.pcm is not None:
            body["pcm_b64"] = base64.b64encode(audio_window.pcm).decode("ascii")
        if audio_window.audio_path:
            body["audio_path"] = audio_window.audio_path
        try:
            resp = client.post(url, json=body)
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"remote_avatar stream_audio request failed: {exc}"
            ) from exc
        _raise_http(resp, "stream_audio")
        data = resp.json() if resp.content else {}
        windows = data.get("video_windows")
        if isinstance(windows, list) and windows:
            for i, w in enumerate(windows):
                if not isinstance(w, dict):
                    continue
                yield VideoWindow(
                    session_id=session_id,
                    utterance_id=audio_window.utterance_id,
                    seq=int(w.get("seq", audio_window.seq if i == 0 else audio_window.seq + i)),
                    frames=w.get("frames") or [],
                    fps=int(w.get("fps", 25)),
                    duration_ms=int(w.get("duration_ms", audio_window.duration_ms)),
                    audio_window_id=str(w.get("audio_window_id") or audio_window.id),
                    is_final=bool(
                        w.get(
                            "is_final",
                            audio_window.is_final and i == len(windows) - 1,
                        )
                    ),
                )
            return
        # Default: one empty-frame window matching the audio window timing.
        yield VideoWindow(
            session_id=session_id,
            utterance_id=audio_window.utterance_id,
            seq=audio_window.seq,
            frames=[],
            fps=25,
            duration_ms=audio_window.duration_ms,
            audio_window_id=audio_window.id,
            is_final=audio_window.is_final,
        )

    def interrupt(self, session_id: str) -> None:
        if session_id not in self._sessions:
            raise KeyError(session_id)
        client = self._get_client()
        url = _join_url(self._base_url, f"/sessions/{session_id}/interrupt")
        try:
            resp = client.post(url, json={"session_id": session_id})
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"remote_avatar interrupt request failed: {exc}"
            ) from exc
        _raise_http(resp, "interrupt")

    def stop(self, session_id: str) -> None:
        if session_id not in self._sessions:
            raise KeyError(session_id)
        client = self._get_client()
        url = _join_url(self._base_url, f"/sessions/{session_id}/stop")
        try:
            resp = client.post(url, json={"session_id": session_id})
        except httpx.RequestError as exc:
            raise RuntimeError(f"remote_avatar stop request failed: {exc}") from exc
        _raise_http(resp, "stop")
        self._sessions.discard(session_id)

    def session_status(self, session_id: str) -> str:
        if session_id not in self._sessions:
            raise KeyError(session_id)
        return "active"

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
