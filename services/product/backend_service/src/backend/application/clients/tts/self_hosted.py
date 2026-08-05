"""Self-host TTS service client — thin HTTP proxy to the TTS service.

Canonical outbound transport (Task 1.22/1.32): calls the self-host
tts_service `/v1/speech` endpoint and returns typed results. No engine
code, no hosted-provider logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import httpx


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

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def synthesize(
        self,
        text: str,
        *,
        voice: str = "",
        language: str = "vi",
        response_format: str = "pcm",
    ) -> TTSResult:
        """Call the self-host TTS service and return PCM16 audio."""
        client = self._get_client()
        url = urljoin(self._base_url + "/", "v1/speech")
        headers: dict[str, str] = {}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        body: dict = {
            "text": text,
            "voice": voice or None,
            "language": language,
            "response_format": response_format,
        }
        try:
            resp = client.post(url, json=body, headers=headers)
        except httpx.RequestError as exc:
            raise TTSClientError(f"self-host TTS request failed: {exc}") from exc
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise TTSClientError(f"self-host TTS failed: HTTP {resp.status_code} {detail}") from exc
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
