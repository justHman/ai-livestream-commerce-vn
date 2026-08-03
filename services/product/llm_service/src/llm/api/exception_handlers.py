"""Centralized error mapping to the stable error envelope.

Validation, typed application errors, and unexpected failures map to
`{"error": {"code": ..., "message": ...}}` without leaking stack traces
or internal details.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from llm.engines.base import EngineError, EngineUnavailable


def _envelope(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message}},
    )


async def _validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return _envelope(
        "validation_error",
        "request validation failed",
        422,
    )


async def _http_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    return _envelope(
        f"http_{exc.status_code}",
        str(exc.detail),
        exc.status_code,
    )


async def _engine_error_handler(request: Request, exc: EngineError) -> JSONResponse:
    return _envelope("engine_error", str(exc), 502)


async def _engine_unavailable_handler(
    request: Request, exc: EngineUnavailable
) -> JSONResponse:
    return _envelope("engine_unavailable", str(exc), 503)


async def _unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
    return _envelope("internal_error", "internal server error", 500)


def register(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(EngineError, _engine_error_handler)
    app.add_exception_handler(EngineUnavailable, _engine_unavailable_handler)
    app.add_exception_handler(Exception, _unexpected_handler)