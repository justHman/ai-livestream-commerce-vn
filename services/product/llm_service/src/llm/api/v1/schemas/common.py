"""Common v1 schemas: error envelope, model info, status."""

from __future__ import annotations


from pydantic import BaseModel, Field


class Error(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: Error


class StatusResponse(BaseModel):
    status: str


class ModelInfo(BaseModel):
    """One served model."""

    id: str
    object: str = "model"
    owned_by: str = "self-host"
    created: int = 0
    engine: str = ""


class ModelListResponse(BaseModel):
    object: str = "list"
    data: list[ModelInfo] = Field(default_factory=list)