"""Self-host TTS service client — thin HTTP proxy to the TTS service.

Canonical outbound transport (Task 1.22/1.32): calls the self-host
tts_service `/v1/speech` endpoint and returns typed results. No engine
code, no hosted-provider logic.

Two wire contracts are supported, selected by the ``TTS_API_STYLE`` env:

  - default (unset / anything else) — the in-repo ``/v1/speech`` contract
    above. Unchanged.
  - ``TTS_API_STYLE=openai_audio_speech`` (opt-in) — the OpenAI-compatible
    ``POST /v1/audio/speech`` endpoint exposed by the VieNeu v3 Turbo GPU
    server. That endpoint takes ``{"model", "voice", "input",
    "response_format", "stream_format", "sample_rate"}``, authenticates with
    ``Authorization: Bearer <token>`` and replies with *raw* PCM s16le mono at
    the requested rate — no WAV header and no ``x-audio-*`` response headers —
    so the sample rate is echoed back from the request and the duration is
    derived from the payload length.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import httpx

#: Env flag selecting the OpenAI-compatible `/v1/audio/speech` contract.
OPENAI_AUDIO_SPEECH_STYLE = "openai_audio_speech"
DEFAULT_MODEL_ID = "vieneu-v3-turbo"
DEFAULT_OPENAI_SAMPLE_RATE = 48_000


logger = logging.getLogger(__name__)


class TTSClientError(RuntimeError):
    """Typed transport failure for a TTS client."""


@dataclass
class TTSResult:
    """Synthesized audio plus metadata (no provider secrets)."""

    pcm16: bytes
    sample_rate: int
    duration_ms: int = 0
    engine: str = ""


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _env_str(name: str) -> str:
    return (os.environ.get(name, "") or "").strip()


def _env_tempo() -> float:
    """``TTS_TEMPO``: playback tempo applied to synthesized speech (1.0 = unchanged).

    The VieNeu GPU server ignores the OpenAI ``speed`` field, so a slower, more natural pace is
    produced here with ffmpeg ``atempo`` (pitch is kept). Out-of-range or invalid values mean 1.0.
    """
    try:
        value = float(_env_str("TTS_TEMPO") or 1.0)
    except ValueError:
        return 1.0
    return value if 0.5 <= value <= 1.5 else 1.0


def change_tempo(pcm16: bytes, rate: int, tempo: float, *, timeout_s: float = 20.0) -> bytes:
    """PCM s16le mono at ``rate`` played at ``tempo`` (0.92 = 8% slower), pitch preserved.

    Any failure (ffmpeg missing, error, timeout, empty output) returns the input unchanged: speech
    at the original pace is better than no speech.
    """
    if tempo == 1.0 or not pcm16:
        return pcm16
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        logger.warning("TTS_TEMPO set but ffmpeg is not installed; speech keeps its original pace")
        return pcm16
    try:
        done = subprocess.run(
            [
                ffmpeg,
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-af",
                f"atempo={tempo:.3f}",
                "-f",
                "s16le",
                "-ar",
                str(rate),
                "-ac",
                "1",
                "pipe:1",
            ],
            input=pcm16,
            capture_output=True,
            timeout=timeout_s,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("TTS tempo change failed; speech keeps its original pace", exc_info=True)
        return pcm16
    return done.stdout or pcm16


def _env_int(name: str, default: int) -> int:
    try:
        value = int(_env_str(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


class SelfHostedTTSClient:
    """HTTP client for the self-host TTS service."""

    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        timeout: float = 60.0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        base = (base_url or os.environ.get("TTS_BASE_URL", "") or "").strip()
        if not base:
            raise TTSClientError("SelfHostedTTSClient needs base_url or env TTS_BASE_URL")
        self._base_url = _strip_trailing_slash(base)
        self._api_key = api_key or os.environ.get("TTS_AUTH_TOKEN", "") or ""
        self._timeout = float(timeout)
        self._client = http_client
        # Opt-in OpenAI-compatible wire contract; anything else keeps /v1/speech.
        self._api_style = _env_str("TTS_API_STYLE").lower()
        self._model_id = _env_str("TTS_MODEL_ID") or DEFAULT_MODEL_ID
        self._voice_id = _env_str("TTS_VOICE_ID")
        self._sample_rate = _env_int("TTS_SAMPLE_RATE", DEFAULT_OPENAI_SAMPLE_RATE)
        self._tempo = _env_tempo()

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    def _post(
        self,
        client: httpx.Client,
        url: str,
        body: dict,
        headers: dict[str, str],
    ) -> httpx.Response:
        """POST + typed error mapping, shared by both wire contracts."""
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise TTSClientError(f"self-host TTS request failed: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise TTSClientError(f"self-host TTS failed: HTTP {resp.status_code} {detail}") from exc
        return resp

    def _synthesize_openai_audio_speech(self, text: str, *, voice: str = "") -> TTSResult:
        """`POST /v1/audio/speech` — raw PCM s16le mono, no x-audio-* headers."""
        client = self._get_client()
        url = urljoin(self._base_url + "/", "v1/audio/speech")
        rate = self._sample_rate
        body: dict = {
            "model": self._model_id,
            "input": text,
            "response_format": "pcm",
            "stream_format": "audio",
            "sample_rate": rate,
        }
        resolved_voice = (voice or self._voice_id or "").strip()
        if resolved_voice:
            body["voice"] = resolved_voice
        resp = self._post(client, url, body, self._auth_headers())
        pcm = change_tempo(resp.content or b"", rate, self._tempo)
        duration_ms = int(len(pcm) / (2 * rate) * 1000)
        return TTSResult(
            pcm16=pcm,
            sample_rate=rate,
            duration_ms=duration_ms,
            engine=self._model_id,
        )

    def synthesize(
        self,
        text: str,
        *,
        voice: str = "",
        language: str = "vi",
        response_format: str = "pcm",
    ) -> TTSResult:
        """Call the self-host TTS service and return PCM16 audio."""
        if self._api_style == OPENAI_AUDIO_SPEECH_STYLE:
            return self._synthesize_openai_audio_speech(text, voice=voice)
        client = self._get_client()
        url = urljoin(self._base_url + "/", "v1/speech")
        body: dict = {
            "text": text,
            "voice": voice or None,
            "language": language,
            "response_format": response_format,
        }
        resp = self._post(client, url, body, self._auth_headers())
        sample_rate = int(resp.headers.get("x-audio-sample-rate", "24000"))
        duration_ms = int(resp.headers.get("x-audio-duration-ms", "0"))
        engine = resp.headers.get("x-audio-engine", "")
        return TTSResult(
            pcm16=resp.content or b"",
            sample_rate=sample_rate,
            duration_ms=duration_ms,
            engine=engine,
        )

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
