"""Centralized error mapping to the stable error envelope for avatar."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from typing import cast

from avatar.engines.base import EngineError, EngineUnavailable


def _envelope(code: str, message: str, http_status: int) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": {"code": code, "message": message}},
    )


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    return _envelope("validation_error", "request validation failed", 422)


async def _http_handler(request: Request, exc: Exception) -> JSONResponse:
    # FastAPI calls the handler with the registered exception instance; the
    # signature must accept Exception for add_exception_handler assignability.
    http_exc = cast(StarletteHTTPException, exc)
    return _envelope(f"http_{http_exc.status_code}", str(http_exc.detail), http_exc.status_code)


async def _engine_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return _envelope("engine_error", str(exc), 502)


async def _engine_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    return _envelope("engine_unavailable", str(exc), 503)


async def _unexpected_handler(request: Request, exc: Exception) -> JSONResponse:
    return _envelope("internal_error", "internal server error", 500)


def register(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_handler)
    app.add_exception_handler(EngineError, _engine_error_handler)
    app.add_exception_handler(EngineUnavailable, _engine_unavailable_handler)
    app.add_exception_handler(Exception, _unexpected_handler)
