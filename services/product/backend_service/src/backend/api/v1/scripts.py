"""backend.api.v1.scripts — pre-live script authoring API (Change B, tasks 11.1-11.11).

Authoring is a REST/JSON control surface under ``/api/v1/script-sets``; it does
NOT reuse the runtime avatar/session WebSocket (Decision 16). Long-running
generation/fix/regeneration commands return ``202 Accepted`` with stable
workflow/batch identifiers; progress is one-way SSE.

The router depends ONLY on the ``ScriptAuthoringService`` protocol (see
``backend.application.script_authoring.service``). The domain implementation
is wired by later clusters; tests inject a minimal in-memory fake. This module
MUST NOT import other ``script_authoring`` modules so the contract stays
decoupled from the domain build-out.

HTTP/domain semantics (Decision 16):
  - malformed request/body  -> normal 4xx (400/404/409/422)
  - deterministic gate completed with violations -> HTTP 200, domain state
    ``gate_failed`` (NOT a transport/schema failure)
  - accepted async generation/fix/regeneration -> 202
  - invalid state transition (e.g. fix on non-failed version) -> 409 with a
    stable domain error code
  - session binding with missing/stale scripts -> 409 + structured details

SSE contract (task 11.10): the first event on any connection is a
``batch.snapshot`` event carrying the current batch state plus a monotonic
``revision``; later ``batch.*``/``product.*``/``segment.*`` events carry the
same stable IDs and an incrementing sequence. Reconnecting clients replay the
snapshot (no new jobs are created). Event payloads never include script text
by default.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.application.script_authoring.service import (
    ScriptAuthoringError,
    ScriptAuthoringService,
)

from .router import router as _router, viewer_auth

logger = logging.getLogger(__name__)

__all__ = ["_router"]


def _service(request: Request) -> ScriptAuthoringService:
    """Resolve the injected ScriptAuthoringService from the request container.

    The service is a container-scoped resource; when no authoring service is
    wired the capability is unavailable (501), matching how other optional
    backend capabilities surface.
    """
    service = getattr(request.app.state.container, "script_authoring_service", None)
    if service is None:
        raise HTTPException(status_code=501, detail="script authoring not enabled")
    return service


# ── Error envelope helper ────────────────────────────────────────────
# The shared exception handlers turn HTTPException into the stable
# {"error": {"code", "message"}} envelope; these helpers keep the mapping in
# one place so domain codes stay stable across handlers.


def _domain_error(status_code: int, code: str, message: str) -> HTTPException:
    """Build an HTTPException carrying the stable structured domain error code.

    The registered handler renders ``detail`` as ``{"error": {"code", "message"}}``.
    """
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def _raise_domain(service_error: ScriptAuthoringError) -> HTTPException:
    if service_error.code == "not_found":
        return _domain_error(404, "not_found", service_error.message)
    if service_error.code in (
        "illegal_transition",
        "stale_revision",
        "fix_not_eligible",
        "missing_or_stale_script",
    ):
        return _domain_error(409, service_error.code, service_error.message)
    if service_error.code == "llm_unavailable":
        return _domain_error(503, "llm_unavailable", service_error.message)
    # Unknown/authoring-unavailable codes surface as 400 by default.
    return _domain_error(400, service_error.code, service_error.message)


# ── Request/response models (stable wire contract) ──────────────────


class LiveSessionBriefIn(BaseModel):
    """Lightweight pre-live brief; no runtime session is created."""

    title: str = Field(min_length=1, max_length=256)
    host_name: str = Field(default="", max_length=128)
    shop_name: str = Field(default="", max_length=256)
    note: str = Field(default="", max_length=2_000)


class ScriptSetCreateIn(BaseModel):
    """Create a ScriptSet aggregate (Decision 1)."""

    name: str = Field(min_length=1, max_length=256)
    transition_policy: Literal["ORDER_AWARE", "ORDER_AGNOSTIC"] = "ORDER_AGNOSTIC"
    product_ids: list[str] = Field(default_factory=list, max_length=200)
    brief: LiveSessionBriefIn | None = None


class ScriptSetPatchIn(BaseModel):
    """PATCH a ScriptSet; any change bumps ``revision`` (optimistic locking)."""

    name: str | None = Field(default=None, min_length=1, max_length=256)
    transition_policy: Literal["ORDER_AWARE", "ORDER_AGNOSTIC"] | None = None
    product_ids: list[str] | None = Field(default=None, max_length=200)
    brief: LiveSessionBriefIn | None = None
    revision: int | None = Field(default=None, ge=0)


class DraftIn(BaseModel):
    """PUT draft: full replacement of the product's draft text (display+spoken).

    ``display_text`` is the user-facing form; ``spoken_text`` is the exact TTS
    form the gate and later approval hash bind to (Decision 4). When only
    ``display_text`` is supplied, the service's deterministic normalization
    derives ``spoken_text``.
    """

    display_text: str = Field(min_length=1, max_length=200_000)
    spoken_text: str | None = Field(default=None, max_length=200_000)
    revision: int | None = Field(default=None, ge=0)


class GenerationPreviewReq(BaseModel):
    """No-LLM preview of planned work (Decision 11)."""

    product_id: str = Field(min_length=1, max_length=128)
    target_duration_s: int = Field(ge=60, le=7_200)


class GenerateReq(BaseModel):
    """Start bounded plan + fixed-K segment generation for one product."""

    target_duration_s: int = Field(ge=60, le=7_200)
    intent: str = Field(default="selling", min_length=1, max_length=128)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class RegenerateReq(BaseModel):
    """Explicit human action: regenerate one segment as a new immutable version."""

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class FixReq(BaseModel):
    """Constrained AI repair for a gate-failed version (Decision 5, Fix contract)."""

    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class ApproveReq(BaseModel):
    """Human approval of the exact current compiled version (Decision 14)."""

    version_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=128)


class BatchGenerateReq(BaseModel):
    """One-click multi-product generation = per-product workflows (Decision 10)."""

    product_ids: list[str] = Field(default_factory=list, max_length=200)
    target_duration_s: int = Field(ge=60, le=7_200)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


class ApproveBatchReq(BaseModel):
    """Approve multiple products; each approval is a separate immutable record."""

    product_ids: list[str] = Field(default_factory=list, max_length=200)
    version_ids: dict[str, str] = Field(default_factory=dict, max_length=200)
    actor: str = Field(min_length=1, max_length=128)


def _idempotency_key(request: Request, body_key: str | None) -> str:
    """Client idempotency identity: header first, then body (task 11.8)."""
    return request.headers.get("idempotency-key") or body_key or ""


# ── ScriptSet CRUD ──────────────────────────────────────────────────


@_router.post("/script-sets", status_code=status.HTTP_201_CREATED)
async def create_script_set(
    req: ScriptSetCreateIn,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Create a pre-live ScriptSet aggregate (task 11.2)."""
    service = _service(request)
    try:
        result = await service.create_script_set(
            name=req.name,
            transition_policy=req.transition_policy,
            product_ids=list(req.product_ids),
            brief=req.brief.model_dump() if req.brief is not None else None,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    return result


@_router.get("/script-sets/{set_id}")
async def get_script_set(
    set_id: str,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Read one ScriptSet aggregate (task 11.2)."""
    service = _service(request)
    try:
        result = await service.get_script_set(set_id=set_id)
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.patch("/script-sets/{set_id}")
async def patch_script_set(
    set_id: str,
    req: ScriptSetPatchIn,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Update ScriptSet metadata with revision/conflict handling (task 11.2).

    ``revision`` is the optimistic-lock token: a mismatch returns 409
    ``stale_revision`` so concurrent edits never silently clobber each other.
    """
    service = _service(request)
    try:
        result = await service.update_script_set(
            set_id=set_id,
            name=req.name,
            transition_policy=req.transition_policy,
            product_ids=list(req.product_ids) if req.product_ids is not None else None,
            brief=req.brief.model_dump() if req.brief is not None else None,
            revision=req.revision,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


# ── Per-product authoring commands ──────────────────────────────────


@_router.put("/script-sets/{set_id}/products/{product_id}/draft")
async def put_draft(
    set_id: str,
    product_id: str,
    req: DraftIn,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Create a new DRAFT version from manual text (task 11.3).

    The draft version is immutable; repeated PUTs create newer versions.
    """
    service = _service(request)
    try:
        result = await service.save_draft(
            set_id=set_id,
            product_id=product_id,
            display_text=req.display_text,
            spoken_text=req.spoken_text,
            revision=req.revision,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post("/script-sets/{set_id}/products/{product_id}/submit")
async def submit_for_gate(
    set_id: str,
    product_id: str,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Submit the current DRAFT to the deterministic ScriptGate (task 11.3).

    Gate completion with violations is HTTP 200 with domain state
    ``gate_failed`` (Decision 16); PASS becomes ``REVIEWABLE``.
    """
    service = _service(request)
    try:
        result = await service.submit_for_gate(set_id=set_id, product_id=product_id)
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post("/script-sets/{set_id}/products/{product_id}/generation-preview")
async def product_generation_preview(
    set_id: str,
    product_id: str,
    req: GenerationPreviewReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """No-LLM preview of planned K and semantic-call budget (task 11.4)."""
    service = _service(request)
    try:
        result = await service.preview_product(
            set_id=set_id,
            product_id=product_id,
            target_duration_s=req.target_duration_s,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post(
    "/script-sets/{set_id}/products/{product_id}/generate",
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_product(
    set_id: str,
    product_id: str,
    req: GenerateReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Start planning + fixed-K segment generation for one product (task 11.5).

    Returns 202 with the workflow id; repeated equivalent requests under the
    same idempotency key return the existing workflow (Decision 12).
    """
    service = _service(request)
    key = _idempotency_key(request, req.idempotency_key)
    try:
        result = await service.start_generation(
            set_id=set_id,
            product_id=product_id,
            target_duration_s=req.target_duration_s,
            intent=req.intent,
            idempotency_key=key,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post(
    "/script-sets/{set_id}/products/{product_id}/segments/{segment_index}/regenerate",
    status_code=status.HTTP_202_ACCEPTED,
)
async def regenerate_segment(
    set_id: str,
    product_id: str,
    segment_index: int,
    req: RegenerateReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Explicit human regeneration of one segment (task 11.6).

    Only the named segment gets a new immutable version; eligibility and
    conflict guards return 409 for ineligible states.
    """
    service = _service(request)
    key = _idempotency_key(request, req.idempotency_key)
    try:
        result = await service.regenerate_segment(
            set_id=set_id,
            product_id=product_id,
            segment_index=segment_index,
            idempotency_key=key,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post(
    "/script-sets/{set_id}/products/{product_id}/fix",
    status_code=status.HTTP_202_ACCEPTED,
)
async def fix_product(
    set_id: str,
    product_id: str,
    req: FixReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Constrained AI repair for a gate-failed version (task 11.6).

    Eligible only for gate-failed immutable versions; otherwise 409
    ``fix_not_eligible``. The fixed result is a new DRAFT that must be
    submitted again — never auto-approved.
    """
    service = _service(request)
    key = _idempotency_key(request, req.idempotency_key)
    try:
        result = await service.fix_with_ai(
            set_id=set_id,
            product_id=product_id,
            idempotency_key=key,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post("/script-sets/{set_id}/products/{product_id}/approve")
async def approve_product(
    set_id: str,
    product_id: str,
    req: ApproveReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Human-only approval of the exact current version (task 11.7).

    Requires an authenticated human actor; approval binds the exact version
    (gate PASS never approves).
    """
    service = _service(request)
    try:
        result = await service.approve_product(
            set_id=set_id,
            product_id=product_id,
            version_id=req.version_id,
            actor=req.actor,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post(
    "/script-sets/{set_id}/generate-batch",
    status_code=status.HTTP_202_ACCEPTED,
)
async def generate_batch(
    set_id: str,
    req: BatchGenerateReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """One-click multi-product generation (task 11.8).

    Idempotency identity: ``Idempotency-Key`` header or body
    ``idempotency_key``. Returns 202 with the batch id, per-product planned
    workflow summary, and total estimated semantic calls.
    """
    service = _service(request)
    key = _idempotency_key(request, req.idempotency_key)
    try:
        result = await service.start_batch_generation(
            set_id=set_id,
            product_ids=list(req.product_ids),
            target_duration_s=req.target_duration_s,
            idempotency_key=key,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


@_router.post("/script-sets/{set_id}/approve-batch")
async def approve_batch(
    set_id: str,
    req: ApproveBatchReq,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Approve multiple products; each approval is a separate immutable record
    (task 11.7). ``version_ids`` maps product_id -> exact version to approve.
    """
    service = _service(request)
    try:
        result = await service.approve_batch(
            set_id=set_id,
            product_ids=list(req.product_ids),
            version_ids=dict(req.version_ids),
            actor=req.actor,
        )
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"script set {set_id} not found")
    return result


# ── Generation batch snapshot / cancel / SSE (tasks 11.9-11.10) ─────


@_router.get("/script-sets/{set_id}/generation-batches/{batch_id}")
async def get_generation_batch(
    set_id: str,
    batch_id: str,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Current snapshot of one generation batch (task 11.9)."""
    service = _service(request)
    try:
        result = await service.get_batch(set_id=set_id, batch_id=batch_id)
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"generation batch {batch_id} not found")
    return result


@_router.post("/script-sets/{set_id}/generation-batches/{batch_id}/cancel")
async def cancel_generation_batch(
    set_id: str,
    batch_id: str,
    request: Request,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Cancel a batch: stop scheduling new semantic calls, preserve completed
    artifacts, emit a terminal event (task 11.9)."""
    service = _service(request)
    try:
        result = await service.cancel_batch(set_id=set_id, batch_id=batch_id)
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if result is None:
        raise _domain_error(404, "not_found", f"generation batch {batch_id} not found")
    return result


async def _event_stream(service: ScriptAuthoringService, set_id: str, batch_id: str) -> Any:
    """Yield ``text/event-stream`` frames: snapshot first, then live events.

    The snapshot carries the batch state plus ``revision``; every event is
    JSON with stable IDs (``set_id``/``batch_id``/``product_id``/optional
    ``segment_index``) and a monotonic ``seq`` for client dedup/recovery.
    Event payloads never contain script text by default (Decision 21).
    """
    snapshot = await service.get_batch_events_snapshot(set_id=set_id, batch_id=batch_id)
    yield f"event: batch.snapshot\ndata: {snapshot}\n\n"
    try:
        async for event in service.stream_batch_events(set_id=set_id, batch_id=batch_id):
            yield f"event: {event['event']}\ndata: {event['data']}\n\n"
    except ScriptAuthoringError as exc:
        # A batch deleted while streaming: emit a stable terminal event rather
        # than an untyped error mid-stream.
        yield (
            "event: batch.error\n"
            f'data: {{"set_id": "{set_id}", "batch_id": "{batch_id}", '
            f'"code": "{exc.code}"}}\n\n'
        )


@_router.get("/script-sets/{set_id}/generation-batches/{batch_id}/events")
async def generation_batch_events(
    set_id: str,
    batch_id: str,
    request: Request,
    _: None = Depends(viewer_auth),
) -> StreamingResponse:
    """SSE stream of generation progress for one batch (task 11.10).

    Reconnect-safe: the first event is always ``batch.snapshot`` with the
    current state and revision; replaying the snapshot never creates new jobs.

    Unknown batches fail with a JSON 404 BEFORE the stream starts (the
    snapshot is resolved here, outside the streaming response).
    """
    service = _service(request)
    try:
        snapshot = await service.get_batch_events_snapshot(set_id=set_id, batch_id=batch_id)
    except ScriptAuthoringError as exc:
        raise _raise_domain(exc) from exc
    if snapshot is None:
        raise _domain_error(404, "not_found", f"generation batch {batch_id} not found")
    return StreamingResponse(
        _event_stream(service, set_id, batch_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
