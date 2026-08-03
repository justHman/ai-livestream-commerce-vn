"""OpenAI-compatible hosted TTS outbound client (backend-owned).

Canonical implementation (Task 1.22/1.32): legacy
`core/tts/adapters/openai_speech.py` becomes a thin delegate.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np

DEFAULT_BASE_URL = "http://localhost:20128"
DEFAULT_MODEL = "edge-tts/vi-VN-NamMinhNeural"


class OpenAISpeechError(RuntimeError):
    """Typed transport failure for the OpenAI Speech client."""


@dataclass
class OpenAISpeechResult:
    pcm: np.ndarray
    sample_rate: int


def _decode_mp3_to_float32(audio_bytes: bytes, sample_rate: int) -> np.ndarray:
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


class OpenAISpeechTTSClient:
    """OpenAI-compatible REST TTS client (sync httpx, backend-owned)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        sample_rate: int = 24_000,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._api_key = (
            api_key
            or os.environ.get("TTS_API_KEY", "")
            or os.environ.get("ELEVENLABS_API_KEY", "")
            or ""
        )
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout)
        self._sample_rate = int(sample_rate)
        self._client = http_client

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def synthesize(self, text: str) -> OpenAISpeechResult:
        client = self._get_client()
        url = f"{self._base_url}/v1/audio/speech"
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body = {"model": self._model, "input": text}
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise OpenAISpeechError(f"openai_speech request failed: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise OpenAISpeechError(
                f"openai_speech failed: HTTP {resp.status_code} {detail}"
            ) from exc
        pcm = _decode_mp3_to_float32(resp.content or b"", self._sample_rate)
        if pcm.size == 0:
            raise OpenAISpeechError("openai_speech: empty audio body")
        return OpenAISpeechResult(pcm=pcm, sample_rate=self._sample_rate)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None