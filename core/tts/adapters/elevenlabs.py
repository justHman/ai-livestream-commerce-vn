"""ElevenLabs TTS client (remote, backend-only).

Calls ElevenLabs REST API: POST /v1/text-to-speech/{voice_id}
with header `xi-api-key: <key>`. Returns MP3 bytes; decoded to
float32 mono PCM via ffmpeg-free path (httpx + audio decode).

ElevenLabs API reference:
  https://api.elevenlabs.io/v1/text-to-speech/{voice_id}

Usage:
    tts = load_engine({
        "engine": "elevenlabs",
        "api_key": os.environ["ELEVENLABS_API_KEY"],
        "voice_id": "21m00Tcm4TlvDq8ikWAM",  # Rachel (default)
        "model_id": "eleven_turbo_v2_5",
        "sample_rate": 24000,
    })
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from typing import Optional

import httpx
import numpy as np

from ..base import AudioChunk, TTSEngine, TTSRequest, register_engine

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
# Premade voice (free-tier API-usable; library voices like Rachel need paid plan).
# Roger — laid-back, casual, resonant. Free tier only allows premade voices via API.
DEFAULT_VOICE_ID = "CwhRBWXzGAHq8TQ4Fs17"  # Roger (premade, free-tier)
DEFAULT_MODEL_ID = "eleven_turbo_v2_5"


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _decode_mp3_to_float32(mp3_bytes: bytes, sample_rate: int) -> np.ndarray:
    """Decode MP3 bytes to float32 mono PCM at sample_rate via ffmpeg.

    Windows: NamedTemporaryFile default delete=True locks the file while open,
    so ffmpeg cannot open it (exit code -4 / "could not open input"). Use
    mkstemp + close-before-subprocess + manual cleanup instead.
    """
    if not mp3_bytes:
        return np.zeros(0, dtype=np.float32)
    import wave

    in_fd, in_path = tempfile.mkstemp(suffix=".mp3")
    out_fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(in_fd)
    os.close(out_fd)
    try:
        with open(in_path, "wb") as inf:
            inf.write(mp3_bytes)
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                in_path,
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-f",
                "wav",
                out_path,
            ],
            check=True,
            capture_output=True,
        )
        with wave.open(out_path, "rb") as wf:
            frames = wf.readframes(wf.getnframes())
            return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32767.0
    finally:
        for p in (in_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


@register_engine("elevenlabs")
class ElevenLabsTTSEngine(TTSEngine):
    """ElevenLabs REST API TTS client (sync httpx)."""

    sample_rate = 24_000

    def __init__(self) -> None:
        self._api_key: str = ""
        self._voice_id: str = DEFAULT_VOICE_ID
        self._model_id: str = DEFAULT_MODEL_ID
        self._base_url: str = DEFAULT_BASE_URL
        self._timeout: float = 30.0
        self._client: Optional[httpx.Client] = None

    @classmethod
    def from_config(cls, cfg: dict) -> "ElevenLabsTTSEngine":
        e = cls()
        e._api_key = str(cfg.get("api_key") or os.environ.get("ELEVENLABS_API_KEY", "") or "")
        if not e._api_key:
            raise ValueError("elevenlabs TTS needs cfg['api_key'] or env ELEVENLABS_API_KEY")
        e._voice_id = str(cfg.get("voice_id") or DEFAULT_VOICE_ID)
        e._model_id = str(cfg.get("model_id") or DEFAULT_MODEL_ID)
        e._base_url = _strip_trailing_slash(cfg.get("base_url") or DEFAULT_BASE_URL)
        e._timeout = float(cfg.get("timeout", 30.0))
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        client = cfg.get("http_client")
        if client is not None:
            e._client = client
        e.name = "elevenlabs"
        return e

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        client = self._get_client()
        url = f"{self._base_url}/v1/text-to-speech/{self._voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        body = {
            "text": req.text,
            "model_id": self._model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise RuntimeError(f"elevenlabs TTS request failed: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise RuntimeError(f"elevenlabs TTS failed: HTTP {resp.status_code} {detail}") from exc
        pcm = _decode_mp3_to_float32(resp.content or b"", self.sample_rate)
        if pcm.size == 0:
            raise RuntimeError("elevenlabs TTS: empty audio body")
        return AudioChunk(pcm=pcm, sample_rate=self.sample_rate)
