"""Common avatar v1 schemas: error envelope."""

from __future__ import annotations

from pydantic import BaseModel


class Error(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: Error