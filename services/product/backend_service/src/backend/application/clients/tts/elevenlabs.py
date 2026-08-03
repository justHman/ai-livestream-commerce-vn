"""ElevenLabs hosted TTS outbound client (backend-owned).

Canonical implementation (Task 1.22/1.32): the legacy
`core/tts/adapters/elevenlabs.py` becomes a thin delegate. Owns protocol,
credentials, bounded timeout, response parsing, and typed transport errors.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

import httpx
import numpy as np

DEFAULT_BASE_URL = "https://api.elevenlabs.io"
DEFAULT_VOICE_ID = "CwhRBWXzGAHq8TQ4Fs17"  # Roger (premade, free-tier)
DEFAULT_MODEL_ID = "eleven_turbo_v2_5"


class ElevenLabsError(RuntimeError):
    """Typed transport failure for the ElevenLabs client."""


@dataclass
class ElevenLabsResult:
    pcm: np.ndarray
    sample_rate: int


def _decode_mp3_to_float32(mp3_bytes: bytes, sample_rate: int) -> np.ndarray:
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


class ElevenLabsTTSClient:
    """ElevenLabs REST API TTS client (sync httpx, backend-owned)."""

    def __init__(
        self,
        *,
        api_key: str = "",
        voice_id: str = DEFAULT_VOICE_ID,
        model_id: str = DEFAULT_MODEL_ID,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        sample_rate: int = 24_000,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        self._api_key = api_key or os.environ.get("ELEVENLABS_API_KEY", "") or ""
        if not self._api_key:
            raise ElevenLabsError(
                "ElevenLabsTTSClient needs api_key or env ELEVENLABS_API_KEY"
            )
        self._voice_id = voice_id
        self._model_id = model_id
        self._base_url = base_url.rstrip("/")
        self._timeout = float(timeout)
        self._sample_rate = int(sample_rate)
        self._client = http_client

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def synthesize(self, text: str) -> ElevenLabsResult:
        client = self._get_client()
        url = f"{self._base_url}/v1/text-to-speech/{self._voice_id}"
        headers = {
            "xi-api-key": self._api_key,
            "Content-Type": "application/json",
            "Accept": "audio/mpeg",
        }
        body = {
            "text": text,
            "model_id": self._model_id,
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise ElevenLabsError(f"elevenlabs request failed: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise ElevenLabsError(
                f"elevenlabs failed: HTTP {resp.status_code} {detail}"
            ) from exc
        pcm = _decode_mp3_to_float32(resp.content or b"", self._sample_rate)
        if pcm.size == 0:
            raise ElevenLabsError("elevenlabs: empty audio body")
        return ElevenLabsResult(pcm=pcm, sample_rate=self._sample_rate)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None