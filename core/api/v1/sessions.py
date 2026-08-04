"""core.api.v1.sessions — /lite/* session lifecycle + /sessions/* aliases."""

from __future__ import annotations

import asyncio

from typing import Any

from fastapi import Depends, HTTPException
from ...config import AppConfig
from ...llm.base import LLMEngine, _NoopEngine
from ...render.base import FullPipelineBackend, StartOptions, StreamingAvatarBackend
from ...render.orchestrator import StreamOrchestrator
from ...render.queue import BoundedVideoQueue, CoordinatorMetrics
from ...tts.base import TTSEngine, ToneEngine
from . import router
from .router import logger
from .router import router as _router  # noqa: F401

async def _persist_viewer_msgs(
    d: router.V1Deps, session_id: str, comments, *, author: str = "viewer"
) -> None:
    """Persist ingested viewer comments to the runtime DB (fire-and-forget).

    No-op when pg_store is None/disabled. Swallows errors so a broken runtime
    DB never breaks the ingest/chat response.
    """
    if d.pg_store is None or not getattr(d.pg_store, "enabled", False):
        return
    for c in comments:
        text = getattr(c, "text", None)
        if not text:
            continue
        try:
            await d.pg_store.insert_viewer_msg(
                session_id,
                text,
                author=author,
                comment_id=None,
                source="platform",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_viewer_msg", session_id
            )


@_router.post("/lite/start")
async def lite_start(
    req: router.StartReq,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    d = router.deps()
    try:
        result = await asyncio.to_thread(
            d.backend.start,
            StartOptions(avatar_id=req.avatar_id, is_sandbox=req.is_sandbox),
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await d.store.set(result.session_id, {"status": "active", "mode": result.mode})
    if d.livekit_publishers is not None:
        d.livekit_publishers.activate(result.session_id)
    if d.pg_store is not None and getattr(d.pg_store, "enabled", False):
        try:
            await d.pg_store.upsert_session(
                result.session_id,
                status="active",
                mode=result.mode,
                render_backend=d.config.render_backend if d.config else None,
                avatar_id=req.avatar_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=upsert_session", result.session_id
            )
    return result.public_dict()  # frontend-safe only


@_router.post("/lite/say")
async def lite_say(
    req: router.SayReq,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    d = router.deps()
    # Phase E: streaming coordinator path for StreamingAvatarBackend (mock +
    # future self-host). FullPipelineBackend (cloud) keeps backend.say().
    if isinstance(d.backend, StreamingAvatarBackend):
        return await _streaming_say(d, req)
    if not isinstance(d.backend, FullPipelineBackend):
        raise HTTPException(status_code=501, detail="backend does not support say()")
    # Cloud / FullPipelineBackend path.
    # Per-session lock: 1 say at a time. LLM remote ~6s/call + LiveAvatar
    # sandbox 1 concurrent — overlapping says overload + 503. Reject 409
    # if a turn is already running; FE reuses the queue + retries next tick.
    sid = req.session_id
    if not d.locks.try_acquire(sid):
        raise HTTPException(status_code=409, detail="already_speaking")
    await d.hub.emit(sid, {"type": "avatar.speak_started", "text": req.text})
    try:
        reply = await asyncio.to_thread(d.backend.say, sid, req.text, req.generate)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    finally:
        d.locks.release(sid)
    await d.hub.emit(sid, {"type": "avatar.speak_ended", "reply": reply})
    return {"ok": True, "reply": reply}


async def _streaming_say(d: router.V1Deps, req: router.SayReq) -> dict[str, Any]:
    """Run the LLM->chunker->TTS->backend streaming coordinator for one turn.

    Per-session lock: if a turn is already running for this session, reject
    with 409 already_speaking. The lock is released in ``finally`` so a
    subsequent say always succeeds once this one finishes.
    """
    sid = req.session_id
    if not d.locks.try_acquire(sid):
        raise HTTPException(status_code=409, detail="already_speaking")

    # Resolve LLM/TTS engines. Mock mode may have none loaded (e.g. offline
    # defaults LLM_ENGINE=none / TTS_ENGINE=tone) — fall back to the built-in
    # offline stubs so /lite/say always works without a model. When a real
    # engine IS configured in mock mode, server.py loads it and em.llm/.tts
    # are non-None (Finding 1).
    em = d.engine_manager
    llm: LLMEngine = em.llm if (em is not None and em.llm is not None) else _NoopEngine()
    tts: TTSEngine = em.tts if (em is not None and em.tts is not None) else ToneEngine()
    # Note: if a real engine was configured but FAILED to load, em.llm/.tts
    # are None and we fall back to the stubs here so /lite/say still responds.
    # /health/ready is the honest signal that the configured engine is broken
    # (Finding 2); this path keeps the server functional for dev/demo.

    # Bounded queue + metrics for this utterance.
    cfg = d.config or AppConfig()
    max_q = getattr(cfg, "avatar_max_queue_windows", 5)
    queue = BoundedVideoQueue(max_size=max_q)
    metrics = CoordinatorMetrics()
    orch_cfg = {
        "text_chunk_min_chars": getattr(cfg, "text_chunk_min_chars", 12),
        "text_chunk_target_chars": getattr(cfg, "text_chunk_target_chars", 40),
        "text_chunk_max_chars": getattr(cfg, "text_chunk_max_chars", 80),
        "text_chunk_flush_timeout_ms": getattr(cfg, "text_chunk_flush_timeout_ms", 350),
    }
    # Widen the try/finally to wrap the speak_started emit, orchestrator
    # construction, run, drain, and return. If the emit raises (e.g. WS
    # error), the finally still releases the per-session lock and cleans up
    # the orchestrator entry — otherwise the session would be permanently
    # locked (every future /lite/say -> 409) and the entry would leak.
    try:
        orchestrator = StreamOrchestrator(
            llm=llm,
            tts=tts,
            backend=d.backend,
            queue=queue,
            metrics=metrics,
            config=orch_cfg,
            audio_window_callback=d.livekit_publishers.publish
            if d.livekit_publishers is not None
            else None,
        )
        # Register the orchestrator so /lite/interrupt can cancel it.
        d.orchestrators[sid] = {"orchestrator": orchestrator, "queue": queue}

        await d.hub.emit(sid, {"type": "avatar.speak_started", "text": req.text})
        if req.generate:
            system_prompt = None
            if em is not None and hasattr(em, "_system_prompt"):
                system_prompt = em._system_prompt or None
            spoken = await orchestrator.run(sid, req.text, system_prompt=system_prompt)
        else:
            spoken = await orchestrator.speak_verbatim(sid, req.text)
        # Drain the queue: emit one WS event per VideoWindow to the control
        # hub so connected clients see frame updates. In production the MEDIA
        # plane carries the actual video; this control event is for telemetry.
        windows_emitted = 0
        while queue.qsize() > 0:
            vw = await queue.get()
            windows_emitted += 1
            await d.hub.emit(
                sid,
                {
                    "type": "avatar.video_window",
                    "seq": vw.seq,
                    "is_final": vw.is_final,
                    "duration_ms": vw.duration_ms,
                },
            )
        await d.hub.emit(sid, {"type": "avatar.speak_ended", "reply": spoken})
        return {
            "ok": True,
            "reply": spoken,
            "windows": windows_emitted,
            "metrics": metrics.to_dict(),
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    finally:
        d.orchestrators.pop(sid, None)
        d.locks.release(sid)


@_router.post("/lite/interrupt")
async def lite_interrupt(req: router.SessionReq, _: None = Depends(router.viewer_auth)) -> dict[str, Any]:
    d = router.deps()
    # Task 8: if there is an active streaming orchestrator for this session,
    # cancel it first (stops emission + drains the bounded queue).
    try:
        if d.coordinator is not None and d.coordinator.has(req.session_id):
            await d.coordinator.interrupt(req.session_id)
        else:
            entry = d.orchestrators.get(req.session_id)
            if entry is not None:
                orch: StreamOrchestrator = entry["orchestrator"]
                await orch.cancel(req.session_id)
            await asyncio.to_thread(d.backend.interrupt, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await d.hub.emit(req.session_id, {"type": "avatar.interrupted"})
    return {"ok": True}


@_router.post("/lite/stop")
async def lite_stop(req: router.SessionReq, _: None = Depends(router.viewer_auth)) -> dict[str, Any]:
    d = router.deps()
    # Wave 2: stop the DirectorCoordinator for this session (before teardown).
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        d.coordinator.stop(req.session_id)
    entry = d.orchestrators.get(req.session_id)
    if entry is not None:
        orchestrator: StreamOrchestrator = entry["orchestrator"]
        await orchestrator.cancel(req.session_id)
    try:
        await asyncio.to_thread(d.backend.stop, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if d.livekit_publishers is not None:
        await d.livekit_publishers.stop(req.session_id)
    if d.director is not None:
        d.director.detach(req.session_id)
    await d.store.delete(req.session_id)
    await d.hub.emit(req.session_id, {"type": "session.stopped"})
    # P4 hardening: drop the per-session lock entry to prevent memory leak.
    d.locks.drop(req.session_id)
    return {"ok": True, "stopped": req.session_id}


# ── Director-driven endpoints (orchestration) ───────────────────────


@_router.post("/lite/attach")
async def lite_attach(req: router.AttachReq, _: None = Depends(router.viewer_auth)) -> dict[str, Any]:
    """Attach a Director to a started session: build the FSM + embed the catalog."""
    d = router.deps()
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    from ...director.catalog import Product

    if await d.store.get(req.session_id) is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    products = [Product(**p.model_dump()) for p in req.products]
    shop_profile = req.shop_profile_text()
    # Re-attach updates the existing runtime/coordinator atomically. Stopping
    # the coordinator here would erase the active checkpoint and rolling window.
    has_coordinator = d.coordinator is not None and d.coordinator.has(req.session_id)
    try:
        runtime_values = (
            req.runtime_config.model_dump(exclude_none=True)
            if req.runtime_config is not None
            else None
        )
        info = await asyncio.to_thread(
            d.director.attach,
            req.session_id,
            products,
            shop_profile=shop_profile,
            run_plan=router.build_run_plan(req.products, persona=shop_profile),
            runtime_config=runtime_values,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # M3: freeze the product snapshot into the runtime DB (fire-and-forget).
    if d.pg_store is not None and getattr(d.pg_store, "enabled", False):
        try:
            await d.pg_store.insert_product_snapshot(
                req.session_id, [p.model_dump() for p in req.products]
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_product_snapshot",
                req.session_id,
            )
    # Set the persona before the Coordinator tick exists. Attach is config-only;
    # the Coordinator stays dormant until the first viewer comment is ingested.
    if shop_profile and hasattr(d.backend, "set_persona"):
        try:
            d.backend.set_persona(req.session_id, shop_profile)
        except Exception:
            logger.warning(
                "set_persona failed session=%s (continuing with default persona)",
                req.session_id,
            )
    if d.coordinator is not None:
        if not has_coordinator:
            d.coordinator.start(
                session_id=req.session_id,
                products=products,
                activated=False,
            )
        else:
            d.coordinator.update_catalog(req.session_id, products)
    return {"ok": True, "will_speak": False, **info}


@_router.patch("/lite/config")
async def lite_config_update(
    req: router.RuntimeConfigUpdateReq,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    d = router.deps()
    values = req.model_dump(exclude={"session_id"}, exclude_none=True)
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        updater = d.coordinator.update_runtime_config
    elif d.director is not None and d.director.has(req.session_id):
        updater = d.director.update_runtime_config
    else:
        raise HTTPException(status_code=409, detail="session not attached")
    try:
        return {"ok": True, "session_id": req.session_id, **updater(req.session_id, values)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@_router.post("/lite/ingest")
async def lite_ingest(
    req: router.IngestReq,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    """Feed viewer comments to the Director; it decides + the avatar speaks.

    This is the closed loop: comments -> cluster/score -> Decision ->
    background streaming pipeline. Frontend just POSTs raw comments; the avatar reacts.

    Wave 2: when a DirectorCoordinator is active for this session, route
    comments through it (async ChatQueue path) instead of the sync Director
    ingest. The coordinator's tick loop will decide and speak asynchronously.
    Falls back to the existing sync Director path when coordinator is None.
    """
    d = router.deps()
    # Wave 2: coordinator path (async tick loop drains the queue).
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        d.coordinator.update_traffic(
            req.session_id,
            viewer_count=req.viewer_count,
            msg_rate=req.msg_rate,
        )
        for c in req.comments:
            d.coordinator.ingest(req.session_id, c.text, author="viewer", ts=c.t)
        await _persist_viewer_msgs(d, req.session_id, req.comments, author="viewer")
        return {"ok": True, "accepted": True, "queue_stats": d.coordinator.stats(req.session_id)}

    # Fallback: original sync Director path.
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    if not d.director.has(req.session_id):
        raise HTTPException(status_code=409, detail="call /lite/attach first")

    raw = [c.model_dump() for c in req.comments]
    await d.hub.emit(req.session_id, {"type": "director.cycle_started"})
    try:
        result = await asyncio.to_thread(
            d.director.ingest, req.session_id, raw, req.viewer_count, req.msg_rate
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await _persist_viewer_msgs(d, req.session_id, req.comments, author="viewer")
    await d.hub.emit(req.session_id, {"type": "director.spoke", **result})
    return {"ok": True, **result}


# ── Wave 2: single-comment chat endpoint (Phase B coordinator) ────────


@_router.post("/lite/chat", status_code=202)
async def lite_chat(
    payload: router.ChatIn,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    """Accept a single viewer chat comment via the DirectorCoordinator.

    Returns 202 Accepted immediately; the coordinator's tick loop processes
    comments asynchronously. Returns 404 if the coordinator is not active or
    the session is not attached.
    """
    d = router.deps()
    if d.coordinator is None or not d.coordinator.has(payload.session_id):
        raise HTTPException(404, "session not attached to coordinator")
    comment = d.coordinator.ingest(
        session_id=payload.session_id,
        text=payload.text,
        author=payload.author,
        ts=payload.ts,
    )
    await _persist_viewer_msgs(
        d,
        payload.session_id,
        [router.CommentIn(text=payload.text, t=payload.ts)],
        author=payload.author,
    )
    return {
        "accepted": True,
        "comment_id": comment.id,
        "queue_stats": d.coordinator.stats(payload.session_id),
    }


# ── Engine management endpoints (runtime LLM/TTS swap) ───────────────


@_router.post("/sessions")
async def sessions_start(
    req: router.StartReq,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_start(req, _)


@_router.post("/sessions/{session_id}/say")
async def sessions_say(
    session_id: str,
    req: router.PathSayReq,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_say(router.SayReq(session_id=session_id, text=req.text, generate=req.generate), _)


@_router.post("/sessions/{session_id}/interrupt")
async def sessions_interrupt(session_id: str, _: None = Depends(router.viewer_auth)) -> dict[str, Any]:
    return await lite_interrupt(router.SessionReq(session_id=session_id), _)


@_router.post("/sessions/{session_id}/stop")
async def sessions_stop(session_id: str, _: None = Depends(router.viewer_auth)) -> dict[str, Any]:
    return await lite_stop(router.SessionReq(session_id=session_id), _)


@_router.post("/sessions/{session_id}/attach")
async def sessions_attach(
    session_id: str, req: router.PathAttachReq, _: None = Depends(router.viewer_auth)
) -> dict[str, Any]:
    return await lite_attach(
        router.AttachReq(
            session_id=session_id,
            products=req.products,
            shop_profile=req.shop_profile,
            runtime_config=req.runtime_config,
        ),
        _,
    )


@_router.post("/sessions/{session_id}/ingest")
async def sessions_ingest(
    session_id: str,
    req: router.PathIngestReq,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_ingest(
        router.IngestReq(
            session_id=session_id,
            comments=req.comments,
            viewer_count=req.viewer_count,
            msg_rate=req.msg_rate,
        ),
        _,
    )


@_router.post("/sessions/{session_id}/chat", status_code=202)
async def sessions_chat(
    session_id: str,
    req: router.PathChatIn,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_chat(
        router.ChatIn(
            session_id=session_id,
            text=req.text,
            author=req.author,
            ts=req.ts,
        ),
        _,
    )


@_router.post("/sessions/{session_id}/plan/create")
async def sessions_plan_create(
    session_id: str,
    req: router.PlanCreateReq | None = None,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    """Generate a minimal deterministic RunPlan and store on the session."""
    d = router.deps()
    meta = await d.store.get(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    body = req or router.PlanCreateReq()
    plan = router.build_run_plan(body.products, persona=body.persona)
    plan_dict = plan.model_dump()
    meta = dict(meta)
    meta["run_plan"] = plan_dict
    await d.store.set(session_id, meta)

    # If director runtime has the session, attach plan + reset cursor.
    if d.director is not None and d.director.has(session_id):
        try:
            ds = d.director._sessions.get(session_id)
            if ds is not None:
                state = ds.director.state
                state.run_plan = plan
                state.cursor.phase = "opening"
                state.cursor.product_idx = 0
                state.cursor.talking_point_idx = 0
                state.covered_points = {}
        except Exception:
            pass
    return {"ok": True, "session_id": session_id, "plan": plan_dict}
