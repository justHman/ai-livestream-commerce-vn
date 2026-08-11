"""TTS synthesis route.

Routes resolve the active engine from a dependency and invoke the typed
base interface directly — no pass-through delegation (Task 1.31).
Validates text/voice/output bounds; safe streaming/chunk semantics.

Change T: `POST /v1/speech` stays the canonical backend-facing path (the
backend caller uses it); `POST /v1/audio/speech` is an alias to the SAME
handler per the Change T spec. The scheduler integration in the runtime
cluster replaces the engine call; request/response shape stays provider-neutral.
"""

from __future__ import annotations

import io
import wave
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from tts.api.dependencies import (
    get_engine,
    get_gpu_concurrency_limiter,
    get_provider,
)
from tts.api.security.authorization import require_scope
from tts.api.security.rate_limit import GPUConcurrencyLimiter
from tts.api.v1.schemas.common import ErrorResponse
from tts.api.v1.schemas.speech import CapabilityResponse, SpeechRequest, SpeechResponse
from tts.engines.base import TTSEngine, TTSRequest
from tts.providers.capabilities import ProviderCapabilities

router = APIRouter()


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
    default to a fresh request id or "anonymous" — the scheduler cluster
    consumes these. Raw text is never placed in headers or logs.
    """
    return {
        "X-Request-Id": body.session_id or uuid4().hex,
        "X-Session-Id": body.session_id or "anonymous",
        "X-Utterance-Id": body.utterance_id or "anonymous",
        "X-Chunk-Seq": str(body.chunk_seq),
    }


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
):
    """Synthesize speech from text.

    Returns raw PCM (int16 mono) by default, or a WAV container when
    `response_format=wav`. Auth, body limit, and GPU concurrency gates run
    before any engine work.
    """
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
