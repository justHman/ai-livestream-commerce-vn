"""OpenAI-compatible TTS client (remote, backend-only).

Calls any server exposing OpenAI audio speech:
  POST {base_url}/v1/audio/speech
  body: {"model": ..., "input": <text>, ...}
  returns: audio bytes (mp3 by default)

Free local proxies (e.g. localhost:20128 with edge-tts/vi-VN-NamMinhNeural)
have no quota limits, unlike ElevenLabs free tier.

Usage:
    tts = load_engine({
        "engine": "openai_speech",
        "api_key": os.environ["TTS_API_KEY"],
        "base_url": "http://localhost:20128",
        "model": "edge-tts/vi-VN-NamMinhNeural",
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

DEFAULT_BASE_URL = "http://localhost:20128"
DEFAULT_MODEL = "edge-tts/vi-VN-NamMinhNeural"


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _decode_mp3_to_float32(audio_bytes: bytes, sample_rate: int) -> np.ndarray:
    """Decode audio bytes (mp3/wav) to float32 mono PCM at sample_rate via ffmpeg.

    Windows: mkstemp + close-before-subprocess + manual cleanup (NamedTemporaryFile
    locks the file on Windows → ffmpeg cannot open it).
    """
    if not audio_bytes:
        return np.zeros(0, dtype=np.float32)
    import wave
    in_fd, in_path = tempfile.mkstemp(suffix=".mp3")
    out_fd, out_path = tempfile.mkstemp(suffix=".wav")
    os.close(in_fd)
    os.close(out_fd)
    try:
        with open(in_path, "wb") as inf:
            inf.write(audio_bytes)
        subprocess.run(
            ["ffmpeg", "-y", "-i", in_path, "-ar", str(sample_rate),
             "-ac", "1", "-f", "wav", out_path],
            check=True, capture_output=True,
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


@register_engine("openai_speech")
class OpenAISpeechTTSEngine(TTSEngine):
    """OpenAI-compatible REST TTS client (sync httpx)."""

    sample_rate = 24_000

    def __init__(self) -> None:
        self._api_key: str = ""
        self._model: str = DEFAULT_MODEL
        self._base_url: str = DEFAULT_BASE_URL
        self._timeout: float = 30.0
        self._client: Optional[httpx.Client] = None

    @classmethod
    def from_config(cls, cfg: dict) -> "OpenAISpeechTTSEngine":
        e = cls()
        e._api_key = str(
            cfg.get("api_key")
            or os.environ.get("TTS_API_KEY")
            or os.environ.get("ELEVENLABS_API_KEY", "")
            or ""
        )
        e._model = str(cfg.get("model") or cfg.get("model_id") or DEFAULT_MODEL)
        e._base_url = _strip_trailing_slash(
            cfg.get("base_url") or DEFAULT_BASE_URL
        )
        e._timeout = float(cfg.get("timeout", 30.0))
        e.sample_rate = int(cfg.get("sample_rate", 24_000))
        client = cfg.get("http_client")
        if client is not None:
            e._client = client
        e.name = "openai_speech"
        return e

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def synthesize(self, req: TTSRequest) -> AudioChunk:
        client = self._get_client()
        url = f"{self._base_url}/v1/audio/speech"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {"model": self._model, "input": req.text}
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"openai_speech TTS request failed: {exc}"
            ) from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise RuntimeError(
                f"openai_speech TTS failed: HTTP {resp.status_code} {detail}"
            ) from exc
        pcm = _decode_mp3_to_float32(resp.content or b"", self.sample_rate)
        if pcm.size == 0:
            raise RuntimeError("openai_speech TTS: empty audio body")
        return AudioChunk(pcm=pcm, sample_rate=self.sample_rate)
