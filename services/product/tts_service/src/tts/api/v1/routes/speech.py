"""TTS synthesis route.

Routes resolve the active engine from a dependency and invoke the typed
base interface directly — no pass-through delegation (Task 1.31).
Validates text/voice/output bounds; safe streaming/chunk semantics.
"""

from __future__ import annotations

import io
import wave

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from tts.api.dependencies import (
    get_engine,
    get_gpu_concurrency_limiter,
)
from tts.api.security.authorization import require_scope
from tts.api.security.rate_limit import GPUConcurrencyLimiter
from tts.api.v1.schemas.common import ErrorResponse
from tts.api.v1.schemas.speech import SpeechRequest, SpeechResponse
from tts.engines.base import TTSEngine, TTSRequest

router = APIRouter()


def _to_tts_request(body: SpeechRequest) -> TTSRequest:
    return TTSRequest(
        text=body.text,
        voice=body.voice,
        language=body.language,
        speed=body.speed,
    )


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
    }
    return StreamingResponse(io.BytesIO(payload), media_type=media_type, headers=headers)


@router.get("/speech/formats")
def list_formats(
    _scope: str = Depends(require_scope("tts.synthesis")),
) -> JSONResponse:
    """Return supported output formats."""
    return JSONResponse({"formats": ["pcm", "wav"]})