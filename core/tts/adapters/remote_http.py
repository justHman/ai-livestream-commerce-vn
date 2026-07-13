"""Remote HTTP TTS client.

Calls a TTS microservice over HTTP and returns float32 mono PCM.
Default path matches OpenAI-style speech endpoints; configurable for
simple internal services that POST /synthesize.

Usage:
    tts = load_engine({
        "engine": "remote_http",  # alias: "remote"
        "base_url": "http://tts:8002",
        "path": "/v1/audio/speech",  # or "/synthesize"
        "sample_rate": 24000,
    })
"""

from __future__ import annotations

import io
import os
import wave
from typing import Optional
from urllib.parse import urljoin

import httpx
import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _join_url(base_url: str, path: str) -> str:
    base = _strip_trailing_slash(base_url) + "/"
    rel = path.lstrip("/")
    return urljoin(base, rel)


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _pcm16_bytes_to_float32(raw: bytes) -> np.ndarray:
    if not raw:
        return np.zeros(0, dtype=np.float32)
    arr = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32767.0
    return arr


def _wav_bytes_to_float32(raw: bytes) -> tuple[np.ndarray, int]:
    """Decode a WAV container; return (float32 mono, sample_rate)."""
    with wave.open(io.BytesIO(raw), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        frames = wf.readframes(n_frames)
    if sampwidth != 2:
        raise RuntimeError(
            f"remote_http TTS: unsupported WAV sample width {sampwidth} "
            "(only 16-bit PCM)"
        )
    pcm = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
    if n_channels > 1:
        pcm = pcm.reshape(-1, n_channels).mean(axis=1)
    return pcm.astype(np.float32), int(framerate)


def _looks_like_wav(raw: bytes) -> bool:
    return len(raw) >= 12 and raw[0:4] == b"RIFF" and raw[8:12] == b"WAVE"


def _decode_audio_body(
    raw: bytes,
    content_type: str,
    default_sr: int,
) -> tuple[np.ndarray, int]:
    ct = (content_type or "").split(";")[0].strip().lower()
    if "wav" in ct or _looks_like_wav(raw):
        return _wav_bytes_to_float32(raw)
    # raw pcm16le / octet-stream / audio/L16 / empty content-type
    if ct in ("", "application/octet-stream", "audio/pcm", "audio/l16", "audio/raw"):
        return _pcm16_bytes_to_float32(raw), default_sr
    if "json" in ct:
        raise RuntimeError(
            "remote_http TTS: expected audio body, got JSON "
            f"({raw[:200]!r})"
        )
    # Fallback: treat as pcm16le
    return _pcm16_bytes_to_float32(raw), default_sr


def _raise_http(resp: httpx.Response) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = (resp.text or "")[:300]
        raise RuntimeError(
            f"remote_http TTS synthesize failed: HTTP {resp.status_code} {detail}"
        ) from exc


@register_engine("remote_http")
class RemoteHTTPTTSEngine(TTSEngine):
    """HTTP client for remote TTS microservices (sync httpx)."""

    sample_rate = 24_000

    def __init__(self) -> None:
        self._base_url: str = ""
        self._path: str = "/v1/audio/speech"
        self._api_key: str = ""
        self._timeout: float = 60.0
        self._format: str = "pcm"  # "pcm" | "wav" preference in request body
        self._client: Optional[httpx.Client] = None

    @classmethod
    def from_config(cls, cfg: dict) -> "RemoteHTTPTTSEngine":
        e = cls()
        base = (
            cfg.get("base_url")
            or os.environ.get("TTS_BASE_URL", "")
            or ""
        )
        base = str(base).strip()
        if not base:
            raise ValueError(
                "remote_http TTS needs cfg['base_url'] or env TTS_BASE_URL"
            )
        e._base_url = _strip_trailing_slash(base)
        e._path = str(cfg.get("path") or "/v1/audio/speech")
        e._api_key = str(
            cfg.get("api_key") or os.environ.get("TTS_API_KEY", "") or ""
        )
        e._timeout = float(cfg.get("timeout", 60.0))
        e._format = str(cfg.get("response_format") or cfg.get("format") or "pcm")
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        client = cfg.get("http_client")
        if client is not None:
            e._client = client
        e.name = "remote_http"
        return e

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        client = self._get_client()
        url = _join_url(self._base_url, self._path)
        body: dict = {
            "input": req.text,
            "text": req.text,
            "voice": req.voice or "default",
            "language": req.language,
            "speed": req.speed,
            "response_format": self._format,
            "sample_rate": self.sample_rate,
        }
        if req.ref_text:
            body["ref_text"] = req.ref_text
        try:
            resp = client.post(
                url,
                json=body,
                headers=_auth_headers(self._api_key),
            )
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"remote_http TTS request failed: {exc}"
            ) from exc
        _raise_http(resp)
        raw = resp.content or b""
        if not raw:
            return AudioChunk(
                pcm=np.zeros(0, dtype=np.float32),
                sample_rate=self.sample_rate,
            )
        try:
            pcm, sr = _decode_audio_body(
                raw,
                resp.headers.get("content-type", ""),
                self.sample_rate,
            )
        except Exception as exc:
            raise RuntimeError(
                f"remote_http TTS could not decode audio body: {exc}"
            ) from exc
        return AudioChunk(pcm=pcm, sample_rate=sr)

    def unload(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


# Alias for ops/docs naming (keep canonical name remote_http).
register_engine("remote")(RemoteHTTPTTSEngine)
RemoteHTTPTTSEngine.name = "remote_http"
