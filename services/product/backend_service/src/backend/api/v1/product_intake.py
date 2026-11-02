"""Product intake: free-form product text -> PROPOSED facts (internal route, flag PRODUCT_INTAKE_ENABLED, default off).

Called by the Go ai-connector with the same internal token as the avatar routes (``X-Livento-Internal-Token``).
The merchant's text and the model's output are never logged and never put in an error message; the response carries
only the proposed facts and per-reason COUNTS of what was dropped. Nothing returned here is an approved claim.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.dependencies import container_from_request
from backend.application.product_intake import IntakeError, extract_facts
from backend.application.script_authoring.generation.ordered_units import BoundedLLM

from .avatar_credentials import internal_token_guard

logger = logging.getLogger(__name__)

internal_router = APIRouter(prefix="/internal/v1")

_CALL_TIMEOUT_S = 60.0
_MAX_OUTPUT_TOKENS = 4096
_TEMPERATURE = 0.1  # extraction, not creative writing
_ERRORS = {
    "empty_input": (400, "INTAKE_INVALID"),
    "input_too_long": (413, "INTAKE_TOO_LARGE"),
    "llm_failed": (502, "INTAKE_LLM_FAILED"),
    "bad_output": (502, "INTAKE_LLM_FAILED"),
}

_BUSY_WAIT_S = 5.0  # how long a request waits for a free slot before INTAKE_BUSY
_gate: asyncio.Semaphore | None = None  # module-level: one process-wide cap on paid LLM calls

_intake_guard = internal_token_guard("intake")


def _slots(request: Request) -> asyncio.Semaphore:
    global _gate
    if _gate is None:
        limit = getattr(container_from_request(request).config, "product_intake_max_concurrency", 4)
        _gate = asyncio.Semaphore(max(1, int(limit)))
    return _gate


class IntakeExtractReq(BaseModel):
    text: str = Field(repr=False)


def _fail(status: int, code: str) -> NoReturn:
    raise HTTPException(status_code=status, detail={"code": code, "message": code})


async def _intake_enabled(request: Request) -> None:
    if not getattr(container_from_request(request).config, "product_intake_enabled", False):
        _fail(503, "INTAKE_DISABLED")


def intake_llm(request: Request) -> Callable[[str], str]:
    """One bounded call to the engine's text function (single attempt, hard deadline). Tests override this."""
    manager = getattr(container_from_request(request), "engine_manager", None)
    if manager is None:
        _fail(502, "INTAKE_LLM_FAILED")
    try:
        fn = manager.get_llm_fn(max_tokens=_MAX_OUTPUT_TOKENS, temperature=_TEMPERATURE)
    except Exception:  # noqa: BLE001 - no usable engine; never echo the engine's message
        _fail(502, "INTAKE_LLM_FAILED")
    return BoundedLLM(
        fn, attempts=1, call_timeout_s=_CALL_TIMEOUT_S, job_deadline_s=_CALL_TIMEOUT_S + 10
    )


@internal_router.post(
    "/product-intake/extract",
    dependencies=[Depends(_intake_guard), Depends(_intake_enabled)],
)
async def extract(
    request: Request, req: IntakeExtractReq, llm: Callable[[str], str] = Depends(intake_llm)
) -> dict[str, object]:
    gate = _slots(request)
    try:
        await asyncio.wait_for(gate.acquire(), timeout=_BUSY_WAIT_S)
    except asyncio.TimeoutError:
        logger.warning("product intake busy chars=%d", len(req.text))
        _fail(503, "INTAKE_BUSY")
    started = time.monotonic()
    try:
        result = await asyncio.to_thread(extract_facts, req.text, llm)
    except IntakeError as exc:
        logger.warning("product intake failed code=%s chars=%d", exc.code, len(req.text))
        status, code = _ERRORS.get(exc.code, (502, "INTAKE_LLM_FAILED"))
        raise HTTPException(
            status_code=status, detail={"code": code, "message": code}
        ) from None  # the cause may carry merchant text
    finally:
        gate.release()
    logger.info(
        "product intake ok chars=%d facts=%d dropped=%d latency_ms=%d",
        len(req.text),
        len(result.facts),
        len(result.dropped),
        int((time.monotonic() - started) * 1000),
    )
    return {
        "facts": [
            {"type": f.type, "text": f.text, "evidence": f.evidence, "flags": list(f.flags)}
            for f in result.facts
        ],
        "dropped_count": len(result.dropped),
        "dropped": [
            {"reason": reason, "count": count}
            for reason, count in sorted(result.dropped_by_reason().items())
        ],
    }
