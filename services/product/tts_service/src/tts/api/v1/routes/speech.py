"""TTS synthesis route.

Change T (tasks 3.2/11.1): `POST /v1/speech` is the canonical backend-facing
path; `POST /v1/audio/speech` is an alias to the SAME handler. When the
scheduler runtime is ready the handler builds one `SynthesisRequest`, submits
it to the runtime, awaits exactly that request's result, and encodes it.
When the runtime is NOT ready the route falls back to the legacy engine path
with a warning — the backend-facing contract stays stable across both states.

Fallback policy (11.1): runtime_ready=False -> legacy engine; ready -> runtime.
The runtime raises provider domain errors (429/408/422/502/503) which the
central exception handlers map to the stable envelope.
"""

from __future__ import annotations

import io
import logging
import time
import wave
from datetime import timedelta
from uuid import uuid4

import numpy as np
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from tts.api.dependencies import (
    get_engine,
    get_gpu_concurrency_limiter,
    get_provider,
    get_runtime,
)
from tts.api.security.authorization import require_scope
from tts.api.security.rate_limit import GPUConcurrencyLimiter
from tts.api.v1.schemas.common import ErrorResponse
from tts.api.v1.schemas.speech import CapabilityResponse, SpeechRequest, SpeechResponse
from tts.engines.base import TTSEngine, TTSRequest
from tts.providers.capabilities import ProviderCapabilities
from tts.providers.models import GenerationConfig, Priority, SynthesisRequest

logger = logging.getLogger("tts.api.v1.routes.speech")

router = APIRouter()


def _log_request_completion(
    body: SpeechRequest,
    request_id: str,
    outcome: str,
    started: float,
    duration_ms: int,
) -> None:
    """Structured per-request trace line (task 12.7) — bounded fields only.

    request/session/utterance ids and chunk seq are bounded identifiers; the
    raw text is deliberately absent. The context manager binds correlation
    ids so the ContextFilter surfaces them as sid/rid on the log line.
    """
    elapsed = round((time.monotonic() - started) * 1000, 2)
    logger.info(
        "synthesis_outcome outcome=%s queue_wait_ms=%s inference_ms=%s audio_seconds=%s",
        outcome,
        elapsed,
        elapsed,
        round(duration_ms / 1000, 3),
        extra={
            "request_id": request_id,
            "event": "synthesis_outcome",
        },
    )


def _to_tts_request(body: SpeechRequest) -> TTSRequest:
    return TTSRequest(
        text=body.text,
        voice=body.voice,
        language=body.language,
        speed=body.speed,
    )


def _tracing_headers(body: SpeechRequest) -> dict[str, str]:
    """Correlate responses to scheduling context.

    Request/session/utterance identifiers echo what the caller supplied and
    default to a fresh request id or "anonymous" — the scheduler consumes
    these. Raw text is never placed in headers or logs.
    """
    return {
        "X-Request-Id": body.session_id or uuid4().hex,
        "X-Session-Id": body.session_id or "anonymous",
        "X-Utterance-Id": body.utterance_id or "anonymous",
        "X-Chunk-Seq": str(body.chunk_seq),
    }


def _build_synthesis_request(body: SpeechRequest, runtime) -> SynthesisRequest:
    """Build the immutable scheduler identity for this HTTP request.

    A fresh request id is generated for every request; session/utterance/
    chunk identity and the voice profile travel through unchanged. The
    deadline is stamped at arrival (runtime clock + service request
    deadline) so the runtime's urgency/sweep logic has a real bound.
    """
    cfg = runtime.config
    now = runtime.now()
    # request_id is a FRESH unique id per HTTP request — it is the admission
    # identity (duplicates are rejected), so it can never be the session id,
    # which repeats across chunks. Session/utterance/chunk stay as metadata.
    return SynthesisRequest(
        request_id=uuid4().hex,
        session_id=body.session_id or "anonymous",
        utterance_id=body.utterance_id or "anonymous",
        chunk_seq=body.chunk_seq,
        input_text=body.text,
        voice_profile_id=body.voice_profile_id or "default",
        style=body.style,
        priority=Priority(body.priority),
        response_format=body.response_format,
        generation_config=GenerationConfig(speed=body.speed),
        submitted_at=now,
        deadline_at=now + timedelta(milliseconds=cfg.request_deadline_ms),
    )


def _encode_result(result) -> tuple[bytes, str]:
    """Encode an AudioResult to (payload bytes, media type).

    Reuses the same WAV/PCM encoding as the legacy path; the provider's
    canonical float32 waveform is converted to int16 PCM first.
    """
    if result.audio_bytes is not None:
        return result.audio_bytes, "audio/wav"
    waveform = result.waveform
    if waveform is None:
        raise RuntimeError("audio result carries neither waveform nor bytes")
    pcm = (np.clip(waveform, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    if result.response_format == "wav":
        return _wav_bytes(pcm, result.sample_rate), "audio/wav"
    return pcm, "audio/pcm"


def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    """Wrap raw int16 PCM in a WAV container (16-bit mono)."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buffer.getvalue()


@router.post(
    "/speech",
    response_model=SpeechResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def synthesize(
    body: SpeechRequest,
    _scope: str = Depends(require_scope("tts.synthesis")),
    engine: TTSEngine = Depends(get_engine),
    limiter: GPUConcurrencyLimiter = Depends(get_gpu_concurrency_limiter),
    runtime=Depends(get_runtime),
):
    """Synthesize speech from text.

    Runtime path (Change T): builds one scheduler request, waits for exactly
    that request's result, and encodes it. Runtime errors surface as stable
    domain codes (429/408/422/502/503). Fallback: when the runtime is not
    ready, the legacy engine path serves with a warning so the backend-facing
    contract never changes shape.
    """
    if runtime is not None:
        started = time.monotonic()
        with limiter:
            sr = _build_synthesis_request(body, runtime)
            result = await runtime.submit(sr)
        payload, media_type = _encode_result(result)
        sample_rate = result.sample_rate
        duration_ms = result.duration_ms
        headers = {
            "X-Audio-Engine": "scheduler",
            "X-Audio-Sample-Rate": str(sample_rate),
            "X-Audio-Duration-Ms": str(duration_ms),
            **_tracing_headers(body),
        }
        _log_request_completion(body, sr.request_id, "completed", started, duration_ms)
        return StreamingResponse(io.BytesIO(payload), media_type=media_type, headers=headers)

    logger.warning(
        "runtime not ready; falling back to legacy engine for session=%s",
        body.session_id or "anonymous",
        extra={"event": "runtime_fallback"},
    )
    with limiter:
        chunk = engine.synthesize(_to_tts_request(body))
    pcm = chunk.to_pcm16_bytes()
    duration_ms = int(len(pcm) / 2 * 1000 / chunk.sample_rate) if chunk.sample_rate else 0

    if body.response_format == "wav":
        payload = _wav_bytes(pcm, chunk.sample_rate)
        media_type = "audio/wav"
    else:
        payload = pcm
        media_type = "audio/pcm"

    headers = {
        "X-Audio-Engine": engine.name,
        "X-Audio-Sample-Rate": str(chunk.sample_rate),
        "X-Audio-Duration-Ms": str(duration_ms),
        **_tracing_headers(body),
    }
    return StreamingResponse(io.BytesIO(payload), media_type=media_type, headers=headers)


# Change T spec path: alias to the canonical handler above. FastAPI reuses
# the same operation function; the OpenAPI contract documents both paths.
router.add_api_route(
    "/audio/speech",
    synthesize,
    methods=["POST"],
    response_model=SpeechResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)


@router.get("/audio/capabilities", response_model=CapabilityResponse)
def audio_capabilities(provider=Depends(get_provider)) -> CapabilityResponse:
    """Return provider-neutral capability facts from the active provider.

    Falls back to the static config-derived stub when the provider is not
    wired (runtime_ready false). No speaker embeddings or reference codes
    ever appear here.
    """
    if provider is None:
        from tts.config import load_runtime_config

        cfg = load_runtime_config()
        caps = ProviderCapabilities(
            provider_name=cfg.provider,
            model_revision=cfg.model_revision,
            sample_rate_hz=48_000,
            supports_native_batch=False,
            max_batch_size=1,
            supports_voice_cloning=False,
            supports_mixed_voice_batch=False,
            supported_styles=("natural",),
            supported_response_formats=("pcm", "wav"),
        )
    else:
        caps = provider.capabilities()
    return CapabilityResponse(
        provider_name=caps.provider_name,
        model_revision=caps.model_revision,
        sample_rate_hz=caps.sample_rate_hz,
        supports_native_batch=caps.supports_native_batch,
        max_batch_size=caps.max_batch_size,
        supports_voice_cloning=caps.supports_voice_cloning,
        supports_mixed_voice_batch=caps.supports_mixed_voice_batch,
        supported_styles=list(caps.supported_styles),
        supported_expressive_cues=list(caps.supported_expressive_cues),
        supported_response_formats=list(caps.supported_response_formats),
    )


@router.get("/speech/formats")
def list_formats(
    _scope: str = Depends(require_scope("tts.synthesis")),
) -> JSONResponse:
    """Return supported output formats."""
    return JSONResponse({"formats": ["pcm", "wav"]})


@router.get("/audio/metrics")
def audio_metrics(
    request: Request,
    _scope: str = Depends(require_scope("tts.synthesis")),
) -> JSONResponse:
    """Return the process metrics snapshot (tasks 12.1-12.6).

    JSON payload: counters, bounded-label request counts, gauges, and
    fixed-bucket histograms. No unbounded identity values (session/request/
    voice-profile ids, raw text) ever appear — task 12.8 asserts this.
    """
    from tts.observability.metrics import get_metrics_registry

    registry = getattr(request.app.state, "metrics", None) or get_metrics_registry()
    return JSONResponse(registry.snapshot())
