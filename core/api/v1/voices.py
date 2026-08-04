"""core.api.v1.voices — runtime LLM/TTS engine management + TTS preview."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel

from ..auth import admin_auth
from .router import router, TTSPresetIn, TTSPreviewReq, deps, logger, rate_limit_admin


class EngineSwapReq(BaseModel):
    engine: str
    model: str = ""
    model_path: str = ""
    device: str = "auto"
    # LLM-specific
    n_ctx: int = 4096
    n_gpu_layers: int = -1
    max_model_len: int = 4096
    max_tokens: int = 128
    temperature: float = 0.7
    quantization: Optional[str] = None
    # TTS-specific
    sample_rate: int = 24000
    ref_audio: Optional[str] = None
    # Extra passthrough
    extra: dict[str, Any] = {}


@router.get("/engines")
async def engines_status(_: None = Depends(admin_auth)) -> dict[str, Any]:
    """List available LLM/TTS presets + currently loaded engines."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    return d.engine_manager.status()


@router.post("/engines/llm")
async def swap_llm(
    req: EngineSwapReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Swap the LLM engine at runtime. Unloads the old model (frees VRAM),
    loads the new one, re-configures the cloud RenderBackend."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    cfg = {
        "engine": req.engine,
        "model": req.model,
        "model_path": req.model_path,
        "device": req.device,
        "n_ctx": req.n_ctx,
        "n_gpu_layers": req.n_gpu_layers,
        "max_model_len": req.max_model_len,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "quantization": req.quantization,
    }
    cfg.update(req.extra)
    await d.hub.broadcast(
        {"type": "engine.llm_swap_started", "engine": req.engine, "model": req.model}
    )
    try:
        info = await asyncio.to_thread(d.engine_manager.load_llm, cfg)
        d.engine_manager.reconfigure_cloud()
    except Exception as exc:
        await d.hub.broadcast({"type": "engine.llm_swap_failed", "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await d.hub.broadcast(
        {"type": "engine.llm_swapped", "engine": info.engine, "model": info.model}
    )
    return {"ok": True, "engine": info.engine, "model": info.model, "name": info.name}


@router.post("/engines/tts")
async def swap_tts(
    req: EngineSwapReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Swap the TTS engine at runtime. Unloads the old model (frees VRAM),
    loads the new one, re-configures the cloud RenderBackend."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    cfg = {
        "engine": req.engine,
        "model": req.model,
        "weights_path": req.model or req.model_path,
        "device": req.device,
        "sample_rate": req.sample_rate,
        "ref_audio": req.ref_audio,
    }
    cfg.update(req.extra)
    await d.hub.broadcast(
        {"type": "engine.tts_swap_started", "engine": req.engine, "model": req.model}
    )
    try:
        info = await asyncio.to_thread(d.engine_manager.load_tts, cfg)
        d.engine_manager.reconfigure_cloud()
    except Exception as exc:
        await d.hub.broadcast({"type": "engine.tts_swap_failed", "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await d.hub.broadcast(
        {
            "type": "engine.tts_swapped",
            "engine": info.engine,
            "model": info.model,
            "sample_rate": info.sample_rate,
        }
    )
    return {
        "ok": True,
        "engine": info.engine,
        "model": info.model,
        "name": info.name,
        "sample_rate": info.sample_rate,
    }


@router.post("/engines/tts/preset")
async def set_tts_preset(
    payload: TTSPresetIn,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Select a TTS preset by id (Phase A dropdown). Updates the EngineManager's
    in-memory TTS config without loading the model. The next ``POST /engines/tts``
    or full reload will apply it."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=503, detail="engine manager not ready")
    try:
        updated = d.engine_manager.apply_tts_preset(payload.preset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset {payload.preset_id}")
    return {"preset_id": payload.preset_id, "tts_cfg": updated}


@router.post("/engines/tts/preview")
async def preview_tts(
    payload: TTSPreviewReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> Response:
    """Synthesize bounded browser-playable WAV without creating an avatar session."""
    import io
    import wave

    d = deps()
    manager = d.engine_manager
    if manager is None or manager.tts is None:
        raise HTTPException(status_code=503, detail="TTS engine not loaded")
    try:
        manager.validate_tts_selection(payload.tts_id, payload.voice_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from ...tts.base import TTSRequest

    try:
        audio = await asyncio.wait_for(
            asyncio.to_thread(
                manager.tts.synthesize,
                TTSRequest(text=payload.text, voice=payload.voice_id),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="TTS preview timed out") from exc
    except Exception as exc:
        logger.warning("TTS preview failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="TTS preview failed") from exc

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(audio.sample_rate)
        wav.writeframes(audio.to_pcm16_bytes())
    return Response(
        content=output.getvalue(),
        media_type="audio/wav",
        headers={
            "X-TTS-Id": payload.tts_id,
            "X-Voice-Id": payload.voice_id,
            "X-Sample-Rate": str(audio.sample_rate),
        },
    )


# ── Debug mode endpoints (mock viewer traffic + products) ─────────────
