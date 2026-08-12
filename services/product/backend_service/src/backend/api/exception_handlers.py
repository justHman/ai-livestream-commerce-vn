"""backend.api.exception_handlers — stable, safe error envelopes.

All API errors use the stable envelope::

    {"error": {"code": "string", "message": "string"}}

Domain errors MAY carry a structured ``details`` object for machine
consumers (e.g. binding-time missing/stale product lists, Decision 16):

    {"error": {"code": "missing_or_stale_script", "message": "...",
               "details": {"missing": [...], "stale": [...]}}}

``details`` is absent for all other errors, so the base envelope is
unchanged. No stack trace, internal path, raw database error, or secret
value leaks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _envelope(
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return JSONResponse(
        status_code=status_code,
        content={"error": error},
    )


def _detail_shape(detail: Any) -> tuple[str, str, Any]:
    """Normalize an HTTPException ``detail`` into (code, message, details).

    - string detail          -> ("http_<status>", detail, None)
    - dict with ``code``     -> (detail["code"], detail["message"], detail.get("details"))
      (the ``api/v1/scripts.py`` ``_domain_error`` shape and the session
      binding endpoint)
    - any other shape        -> ("http_<status>", str(detail), None)
    """
    if isinstance(detail, str):
        return "", detail, None
    if isinstance(detail, dict) and "code" in detail:
        return str(detail["code"]), str(detail.get("message", "")), detail.get("details")
    return "", str(detail or ""), None


async def _http_exception_handler(request: Request, exc) -> JSONResponse:
    code, message, details = _detail_shape(exc.detail)
    if not code:
        code = f"http_{exc.status_code}"
    return _envelope(exc.status_code, code, message, details)


async def _request_validation_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    if any(error["type"] == "string_too_long" for error in exc.errors()):
        return _envelope(413, "input_too_long", "input too long")
    return await request_validation_exception_handler(request, exc)


async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception method=%s path=%s error_type=%s",
        request.method,
        request.url.path,
        type(exc).__name__,
    )
    return _envelope(500, "internal_error", "internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    """Register the canonical error envelope handlers on ``app``."""
    from starlette.exceptions import HTTPException as StarletteHTTPException

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
