"""backend.api.v1.sessions — /sessions/* session lifecycle (canonical copy).

Copied from ``core/api/v1/sessions.py`` (COPY-DON'T-IMPORT, Task 1.25) minus
the legacy ``/lite/*`` aliases and the process-global ``v1.deps()`` seam.
Dependencies come from the typed ``BootstrapContainer`` via
``backend.api.dependencies.container_from_request``.
"""

from __future__ import annotations

import asyncio

from typing import Any

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.dependencies import container_from_request

from . import router
from .router import logger
from .router import router as _router  # noqa: F401


def _container(request: Request):
    return container_from_request(request)


@_router.post("/sessions")
async def sessions_start(
    req: router.StartReq,
    request: Request,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    d = _container(request)
    from backend.application.render.engines_base import StartOptions

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
                "Postgres persistence failed session=%s operation=upsert_session",
                result.session_id,
            )
    return result.public_dict()  # frontend-safe only


@_router.post("/sessions/{session_id}/say")
async def sessions_say(
    session_id: str,
    req: router.PathSayReq,
    request: Request,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    return await _say(
        request,
        router.SayReq(session_id=session_id, text=req.text, generate=req.generate),
    )


async def _say(request: Request, req: router.SayReq) -> dict[str, Any]:
    d = _container(request)
    from backend.application.render.engines_base import FullPipelineBackend, StreamingAvatarBackend

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
    if d.hub is not None:
        await d.hub.emit(sid, {"type": "avatar.speak_started", "text": req.text})
    try:
        reply = await asyncio.to_thread(d.backend.say, sid, req.text, req.generate)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    finally:
        d.locks.release(sid)
    if d.hub is not None:
        await d.hub.emit(sid, {"type": "avatar.speak_ended", "reply": reply})
    return {"ok": True, "reply": reply}


async def _streaming_say(d: Any, req: router.SayReq) -> dict[str, Any]:
    """Run the LLM->chunker->TTS->backend streaming coordinator for one turn.

    Per-session lock: if a turn is already running for this session, reject
    with 409 already_speaking. The lock is released in ``finally`` so a
    subsequent say always succeeds once this one finishes.
    """
    from backend.application.render.queue import BoundedVideoQueue, CoordinatorMetrics
    from backend.application.render.orchestrator import (
        StreamOrchestrator,
        StreamingControllerConfig,
    )
    from backend.application.text_chunker import FixedChunkPolicyConfig
    from llm.engines.base import LLMEngine, _NoopEngine
    from tts.engines.base import TTSEngine, ToneEngine

    sid = req.session_id
    if not d.locks.try_acquire(sid):
        raise HTTPException(status_code=409, detail="already_speaking")

    # Resolve LLM/TTS engines. Mock mode may have none loaded (e.g. offline
    # defaults LLM_ENGINE=none / TTS_ENGINE=tone) — fall back to the built-in
    # offline stubs so /lite/say always works without a model.
    em = d.engine_manager
    llm: LLMEngine = em.llm if (em is not None and em.llm is not None) else _NoopEngine()
    tts: TTSEngine = em.tts if (em is not None and em.tts is not None) else ToneEngine()

    # Bounded queue + metrics for this utterance. The chunking config is
    # built ONCE from AppConfig into the typed configs the orchestrator
    # consumes (no dict passthrough, no duplicated defaults).
    cfg = d.config or router.AppConfig()
    max_q = getattr(cfg, "avatar_max_queue_windows", 5)
    queue = BoundedVideoQueue(max_size=max_q)
    metrics = CoordinatorMetrics()
    fixed_config = FixedChunkPolicyConfig(
        min_chars=getattr(cfg, "text_chunk_min_chars", FixedChunkPolicyConfig().min_chars),
        target_chars=getattr(cfg, "text_chunk_target_chars", FixedChunkPolicyConfig().target_chars),
        max_chars=getattr(cfg, "text_chunk_max_chars", FixedChunkPolicyConfig().max_chars),
    )
    controller_config = StreamingControllerConfig(
        flush_timeout_ms=getattr(
            cfg, "text_chunk_flush_timeout_ms", StreamingControllerConfig().flush_timeout_ms
        ),
    )
    try:
        orchestrator = StreamOrchestrator(
            llm=llm,
            tts=tts,
            backend=d.backend,
            queue=queue,
            metrics=metrics,
            fixed_config=fixed_config,
            controller_config=controller_config,
            audio_window_callback=d.livekit_publishers.publish
            if d.livekit_publishers is not None
            else None,
        )
        # Register the orchestrator so /sessions/{id}/interrupt can cancel it.
        d.orchestrators[sid] = {"orchestrator": orchestrator, "queue": queue}

        if d.hub is not None:
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
            if d.hub is not None:
                await d.hub.emit(
                    sid,
                    {
                        "type": "avatar.video_window",
                        "seq": vw.seq,
                        "is_final": vw.is_final,
                        "duration_ms": vw.duration_ms,
                    },
                )
        if d.hub is not None:
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


@_router.post("/sessions/{session_id}/interrupt")
async def sessions_interrupt(
    session_id: str,
    request: Request,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    d = _container(request)
    # Task 8: if there is an active streaming orchestrator for this session,
    # cancel it first (stops emission + drains the bounded queue).
    try:
        if d.coordinator is not None and d.coordinator.has(session_id):
            await d.coordinator.interrupt(session_id)
        else:
            entry = d.orchestrators.get(session_id)
            if entry is not None:
                orch = entry["orchestrator"]
                await orch.cancel(session_id)
            await asyncio.to_thread(d.backend.interrupt, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if d.hub is not None:
        await d.hub.emit(session_id, {"type": "avatar.interrupted"})
    return {"ok": True}


@_router.post("/sessions/{session_id}/stop")
async def sessions_stop(
    session_id: str,
    request: Request,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    d = _container(request)
    # Wave 2: stop the DirectorCoordinator for this session (before teardown).
    if d.coordinator is not None and d.coordinator.has(session_id):
        d.coordinator.stop(session_id)
    entry = d.orchestrators.get(session_id)
    if entry is not None:
        orchestrator = entry["orchestrator"]
        await orchestrator.cancel(session_id)
    try:
        await asyncio.to_thread(d.backend.stop, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if d.livekit_publishers is not None:
        await d.livekit_publishers.stop(session_id)
    if d.director is not None:
        d.director.detach(session_id)
    await d.store.delete(session_id)
    if d.hub is not None:
        await d.hub.emit(session_id, {"type": "session.stopped"})
    # P4 hardening: drop the per-session lock entry to prevent memory leak.
    d.locks.drop(session_id)
    return {"ok": True, "stopped": session_id}


# ── Director-driven endpoints (orchestration) ───────────────────────


@_router.post("/sessions/{session_id}/attach")
async def sessions_attach(
    session_id: str,
    req: router.PathAttachReq,
    request: Request,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    """Attach a Director to a started session: build the FSM + embed the catalog."""
    d = _container(request)
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    from backend.application.director.catalog import Product

    if await d.store.get(session_id) is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    products = [Product(**p.model_dump()) for p in req.products]
    shop_profile = req.shop_profile_text()
    # Re-attach updates the existing runtime/coordinator atomically. Stopping
    # the coordinator here would erase the active checkpoint and rolling window.
    has_coordinator = d.coordinator is not None and d.coordinator.has(session_id)
    try:
        runtime_values = (
            req.runtime_config.model_dump(exclude_none=True)
            if req.runtime_config is not None
            else None
        )
        info = await asyncio.to_thread(
            d.director.attach,
            session_id,
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
                session_id, [p.model_dump() for p in req.products]
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_product_snapshot",
                session_id,
            )
    # Set the persona before the Coordinator tick exists. Attach is config-only;
    # the Coordinator stays dormant until the first viewer comment is ingested.
    if shop_profile and hasattr(d.backend, "set_persona"):
        try:
            d.backend.set_persona(session_id, shop_profile)
        except Exception:
            logger.warning(
                "set_persona failed session=%s (continuing with default persona)",
                session_id,
            )
    if d.coordinator is not None:
        if not has_coordinator:
            d.coordinator.start(
                session_id=session_id,
                products=products,
                activated=False,
            )
        else:
            d.coordinator.update_catalog(session_id, products)
    return {"ok": True, "will_speak": False, **info}


@_router.patch("/sessions/{session_id}/config")
async def sessions_config(
    session_id: str,
    req: router.PathRuntimeConfigUpdateReq,
    request: Request,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    """Canonical path-style runtime config update."""
    d = _container(request)
    values = req.model_dump(exclude_none=True)
    if d.coordinator is not None and d.coordinator.has(session_id):
        updater = d.coordinator.update_runtime_config
    elif d.director is not None and d.director.has(session_id):
        updater = d.director.update_runtime_config
    else:
        raise HTTPException(status_code=409, detail="session not attached")
    try:
        return {"ok": True, "session_id": session_id, **updater(session_id, values)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ── Canonical multi-platform event ingress (OpenSpec 2.2) ────────────


@_router.post("/sessions/{session_id}/events")
async def sessions_events(
    session_id: str,
    req: router.EventsIn,
    request: Request,
    _: None = Depends(router.viewer_auth),
    _limit: None = Depends(router.rate_limit_viewer),
) -> dict[str, Any]:
    """Canonical viewer ingress: one canonical endpoint for one-or-many events.

    The request body is a bounded list of normalized ``PlatformEvent``
    values; a single-event request uses the same schema and service path as
    a multi-event batch. Every per-event outcome (accepted / duplicate /
    rejected) is a valid batch result -> 200. 404 when the session is
    unknown (no session meta AND no coordinator session).
    """
    d = _container(request)
    service = getattr(d, "event_ingestion", None)
    if service is None:
        raise HTTPException(status_code=501, detail="event ingestion not enabled")
    try:
        return await service.ingest(session_id, req.events)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")


@_router.post("/sessions/{session_id}/plan/create")
async def sessions_plan_create(
    request: Request,
    session_id: str,
    req: router.PlanCreateReq | None = None,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    """Generate a minimal deterministic RunPlan and store on the session."""
    d = _container(request)
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


# ── ScriptSet binding (Change B, task 12.2) ─────────────────────────


class ScriptSetBindReq(BaseModel):
    """Bind a pre-live ScriptSet to a runtime session (Decision 17)."""

    script_set_id: str = Field(min_length=1, max_length=128)


@_router.put("/sessions/{session_id}/script-set")
async def sessions_bind_script_set(
    session_id: str,
    req: ScriptSetBindReq,
    request: Request,
    _: None = Depends(router.viewer_auth),
) -> dict[str, Any]:
    """Bind a ScriptSet to a live session when its scripts are ready.

    Validates existence, required products, approved versions, dependency
    freshness, transition/order compatibility, and runtime catalog
    compatibility. On success stores the binding snapshot (script_set_id +
    product -> approved version + exact spoken_text) in session state
    WITHOUT mutating authoring artifacts (task 12.3).

    Not-ready sets return HTTP 409 with structured ``missing``/``stale``/
    ``incompatible``/``unapproved`` product details (Decision 16).
    """
    d = _container(request)
    meta = await d.store.get(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown session_id")

    from backend.application.script_authoring.runtime_handoff import (
        RuntimeCatalogProxy,
        RuntimePlan,
        build_binding_snapshot,
        resolve_approved_script,
        validate_binding,
    )

    # The authoring service is the source of truth for the aggregate and its
    # approvals. When no authoring service is wired the capability is
    # unavailable (501), matching the /script-sets surface. The service
    # satisfies the BindingSource protocol structurally; recorded dependency
    # fingerprints come from the service's approval records (task 4.4) and
    # are absent (no staleness signal) until that cluster lands.
    source = getattr(request.app.state.container, "script_authoring_service", None)
    if source is None:
        raise HTTPException(status_code=501, detail="script authoring not enabled")
    recorded_dependencies = getattr(source, "recorded_dependencies_by_item", None)

    check = await validate_binding(
        script_set_id=req.script_set_id,
        source=source,
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=RuntimeCatalogProxy(
            getattr(d, "director", None),
            getattr(d, "coordinator", None),
        ),
        requested_products=None,
        recorded_dependencies_by_item=recorded_dependencies,
    )
    if not check.ok:
        # Stable domain error code + structured missing/stale details
        # (Decision 16); the shared handler renders them in the envelope.
        raise HTTPException(
            status_code=409,
            detail={
                "code": "missing_or_stale_script",
                "message": "script set is not ready for binding",
                "details": check.as_dict(),
            },
        )

    # Persist the binding snapshot into session state (task 12.3). The
    # snapshot is derived from approved artifacts; authoring rows are never
    # mutated.
    entries = []
    for product_id in check.script_set.product_ids:
        entry = await resolve_approved_script(
            source,
            script_set_id=req.script_set_id,
            product_id=product_id,
        )
        if entry is not None:
            entries.append(entry)
    snapshot = build_binding_snapshot(script_set_id=req.script_set_id, entries=entries)
    meta = dict(meta)
    meta["script_set_binding"] = snapshot.as_dict()
    await d.store.set(session_id, meta)
    return {"ok": True, "session_id": session_id, "binding": snapshot.as_dict()}
