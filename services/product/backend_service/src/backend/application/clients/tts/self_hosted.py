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
import socket
import subprocess
import json
import threading
import unicodedata
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urljoin

import httpx

#: Env flag selecting the OpenAI-compatible `/v1/audio/speech` contract.
OPENAI_AUDIO_SPEECH_STYLE = "openai_audio_speech"
DEFAULT_MODEL_ID = "vieneu-v3-turbo"
DEFAULT_OPENAI_SAMPLE_RATE = 48_000


logger = logging.getLogger(__name__)

# One dropped request must not end a live: a transient failure (network, 429, 5xx) is retried
# after these pauses; the total stays well below the speech watchdog.
_RETRY_DELAYS_S = (0.4, 1.2)
FIRST_BYTE_DEADLINE_S = 8.0
TOTAL_DEADLINE_MIN_S = 12.0
TOTAL_DEADLINE_PER_CHAR_S = 0.35
TOTAL_DEADLINE_MAX_S = 45.0
PHRASE_WAIT_MARGIN_S = 25.0  # Includes the optional 20 s tempo conversion.


def request_deadline_s(char_count: int) -> float:
    return min(
        TOTAL_DEADLINE_MAX_S, max(TOTAL_DEADLINE_MIN_S, TOTAL_DEADLINE_PER_CHAR_S * char_count)
    )


def phrase_wait_budget_s(char_count: int) -> float:
    return (
        request_deadline_s(char_count) * (len(_RETRY_DELAYS_S) + 1)
        + sum(_RETRY_DELAYS_S)
        + PHRASE_WAIT_MARGIN_S
    )


class TTSClientError(RuntimeError):
    """Typed transport failure for a TTS client."""


VOICES_MAX = 200
VOICES_INSPECT_MAX = 1_000
VOICES_BODY_MAX_BYTES = 1_048_576
_VOICE_FIELD_MAX = 128


def _voice_str(value: object) -> str | None:
    """Trimmed string, or None when it is not a string, is empty, or has control/format characters."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    # Unicode category C* covers C0/C1 controls, bidi and other format characters, surrogates, private use.
    if not value or any(unicodedata.category(c).startswith("C") for c in value):
        return None
    return value


def _first_str(item: dict, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _voice_str(item.get(key))
        if value is not None:
            return value
    return None


def _bounded(value: str | None) -> str | None:
    return value[:_VOICE_FIELD_MAX] if value is not None else None


def parse_voices(payload: object) -> list[dict]:
    """Tolerant parse of a ``/v1/voices`` body: a list, ``{"data": [...]}`` or ``{"voices": [...]}``.

    VieNeu v3 Turbo answers ``{"object": "list", "data": [{"id", "name", "gender"}]}``; the parser stays tolerant, and only
    id/name/language/gender are ever read. At most VOICES_INSPECT_MAX entries are looked at.
    """
    if isinstance(payload, dict):
        payload = payload.get("data", payload.get("voices"))
    if not isinstance(payload, list):
        raise TTSClientError("self-host TTS voices: unexpected response shape")
    voices: list[dict] = []
    seen: set[str] = set()
    for item in payload[:VOICES_INSPECT_MAX]:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            continue
        voice_id = _first_str(item, ("id", "voice_id", "voice", "name"))
        if voice_id is None or len(voice_id) > _VOICE_FIELD_MAX or voice_id in seen:
            continue
        seen.add(voice_id)
        name = _first_str(item, ("display_name", "label", "name", "id")) or voice_id
        voices.append(
            {
                "id": voice_id,
                "name": name[:_VOICE_FIELD_MAX],
                "language": _bounded(_first_str(item, ("language", "lang", "locale"))),
                "gender": _bounded(_first_str(item, ("gender", "sex"))),
            }
        )
        if len(voices) >= VOICES_MAX:
            break
    return voices


_synthesis_cancel: ContextVar[threading.Event | None] = ContextVar(
    "tts_synthesis_cancel", default=None
)


@contextmanager
def synthesis_cancellation(event: threading.Event):
    """Let an abandoned worker finish its attempt, but never start another retry."""
    token = _synthesis_cancel.set(event)
    try:
        yield
    finally:
        _synthesis_cancel.reset(token)


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
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            last = attempt == len(_RETRY_DELAYS_S)
            started = time.monotonic()
            try:
                resp = client.post(url, json=body, headers=headers)
            except httpx.RequestError as exc:
                if last:
                    raise TTSClientError(f"self-host TTS request failed: {exc}") from exc
            else:
                try:
                    resp.raise_for_status()
                    return resp
                except httpx.HTTPStatusError as exc:
                    transient = resp.status_code == 429 or resp.status_code >= 500
                    if last or not transient:
                        detail = (resp.text or "")[:300]
                        raise TTSClientError(
                            f"self-host TTS failed: HTTP {resp.status_code} {detail}"
                        ) from exc
            logger.warning(
                "self-host TTS retry attempt=%s elapsed_s=%.3f input_chars=%s",
                attempt + 1,
                time.monotonic() - started,
                len(body.get("text", "")),
            )
            time.sleep(_RETRY_DELAYS_S[attempt])
        raise AssertionError("unreachable")  # the loop returns or raises on its last attempt

    def _stream_openai_pcm(self, url: str, body: dict) -> bytes:
        """Bound each attempt and discard its connection, including on incomplete bodies."""
        char_count = len(body["input"])
        budget = request_deadline_s(char_count)
        cancelled = _synthesis_cancel.get()
        for attempt in range(len(_RETRY_DELAYS_S) + 1):
            if cancelled is not None and cancelled.is_set():
                raise TTSClientError("TTS synthesis abandoned")
            started = time.monotonic()
            deadline = started + budget
            first_deadline = min(deadline, started + FIRST_BYTE_DEADLINE_S)
            timeout = httpx.Timeout(
                connect=min(5.0, budget),
                read=min(FIRST_BYTE_DEADLINE_S, budget),
                write=min(10.0, budget),
                pool=min(5.0, budget),
            )
            # Owned clients have a separate pool per attempt. Connection: close also prevents
            # reuse when a client is injected (e.g. by tests), even after a successful response.
            owned = self._client is None
            client = None
            socket_holder = [None]
            socket_lock = threading.Lock()
            expired = threading.Event()

            def interrupt_read() -> None:
                # shutdown wakes a blocking recv on Windows; close alone need not.
                with socket_lock:
                    expired.set()
                    sock = socket_holder[0]
                    if sock is not None:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            def capture_socket(stream) -> None:
                if stream is None:
                    return
                with socket_lock:
                    sock = stream.get_extra_info("socket")
                    socket_holder[0] = sock
                    # The deadline can expire before connect completes. Do not let the
                    # newly connected socket enter an unbounded header read in that case.
                    if expired.is_set() and sock is not None:
                        try:
                            sock.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass

            def trace(event: str, info: dict) -> None:
                if event in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
                    capture_socket(info["return_value"])

            def arm(end: float) -> threading.Timer:
                watchdog = threading.Timer(max(0.0, end - time.monotonic()), interrupt_read)
                watchdog.daemon = True
                watchdog.start()
                return watchdog

            # Arm before creating the client or entering stream(): headers are part of
            # the first-body-byte budget, not a separate per-read timeout allowance.
            timer = arm(first_deadline)
            try:
                client = self._client if not owned else httpx.Client(timeout=timeout)
                with client.stream(
                    "POST",
                    url,
                    json=body,
                    headers={**self._auth_headers(), "Connection": "close"},
                    timeout=timeout,
                    extensions={"trace": trace},
                ) as response:
                    capture_socket(response.extensions.get("network_stream"))
                    if expired.is_set() or time.monotonic() >= first_deadline:
                        raise httpx.ReadTimeout("TTS response deadline exceeded")
                    response.raise_for_status()
                    chunks = []
                    first = True
                    for chunk in response.iter_bytes():
                        now = time.monotonic()
                        if now >= deadline or (first and now >= first_deadline):
                            raise httpx.ReadTimeout("TTS response deadline exceeded")
                        if chunk:
                            if first:
                                timer.cancel()
                                timer.join()
                                timer = arm(deadline)
                                first = False
                            chunks.append(chunk)
                    if time.monotonic() >= deadline or (
                        first and time.monotonic() >= first_deadline
                    ):
                        raise httpx.ReadTimeout("TTS response deadline exceeded")
                    return b"".join(chunks)
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                status = (
                    exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
                )
                if attempt == len(_RETRY_DELAYS_S) or (
                    status is not None and status != 429 and status < 500
                ):
                    reason = (
                        f"HTTP {status}" if status is not None else "transport/deadline failure"
                    )
                    raise TTSClientError(f"self-host TTS failed: {reason}") from exc
                logger.warning(
                    "self-host TTS retry attempt=%s elapsed_s=%.3f input_chars=%s",
                    attempt + 1,
                    time.monotonic() - started,
                    char_count,
                )
            finally:
                if timer is not None:
                    timer.cancel()
                    timer.join()
                if owned and client is not None:
                    client.close()
            if cancelled is not None:
                if cancelled.wait(_RETRY_DELAYS_S[attempt]):
                    raise TTSClientError("TTS synthesis abandoned")
            else:
                time.sleep(_RETRY_DELAYS_S[attempt])
        raise AssertionError("unreachable")

    def _synthesize_openai_audio_speech(self, text: str, *, voice: str = "") -> TTSResult:
        """`POST /v1/audio/speech` — raw PCM s16le mono, no x-audio-* headers."""
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
        pcm = change_tempo(self._stream_openai_pcm(url, body), rate, self._tempo)
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

    def list_voices(self) -> list[dict]:
        """``GET /v1/voices`` -> [{"id", "name", "language", "gender"}]; raises TTSClientError."""
        url = urljoin(self._base_url + "/", "v1/voices")
        try:
            with self._get_client().stream(
                "GET", url, headers=self._auth_headers(), timeout=5.0
            ) as resp:
                resp.raise_for_status()
                body = bytearray()
                # small decoded chunks, so the cap is a real memory bound (a transport may yield one huge chunk otherwise)
                for chunk in resp.iter_bytes(chunk_size=65_536):
                    if len(body) + len(chunk) > VOICES_BODY_MAX_BYTES:
                        raise TTSClientError("self-host TTS voices: response too large")
                    body.extend(chunk)
            payload = json.loads(bytes(body))
        except (httpx.HTTPError, ValueError) as exc:
            raise TTSClientError(f"self-host TTS voices failed: {type(exc).__name__}") from exc
        return parse_voices(payload)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
