"""backend.api.exception_handlers — stable, safe error envelopes.

All API errors use the stable envelope::

    {"error": {"code": "string", "message": "string"}}

No stack trace, internal path, raw database error, or secret value leaks.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _envelope(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )


async def _http_exception_handler(request: Request, exc) -> JSONResponse:
    detail = exc.detail if isinstance(exc.detail, str) else str(exc.detail or exc.status_code)
    return _envelope(exc.status_code, f"http_{exc.status_code}", detail)


async def _request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
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