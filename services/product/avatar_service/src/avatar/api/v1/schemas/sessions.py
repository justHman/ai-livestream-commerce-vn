"""Avatar session schemas — browser-safe DTOs only.

The start response exposes only `livekit_url` and `livekit_client_token`
after authorization (Task 1.30/1.32). Provider session IDs, API tokens,
and secrets never appear here or in logs.
"""

from __future__ import annotations


from pydantic import BaseModel, Field


class SessionCreateRequest(BaseModel):
    avatar_id: str = Field(default="default", min_length=1, max_length=128)
    is_sandbox: bool = True
    extra: dict = Field(default_factory=dict)


class SessionStartResponse(BaseModel):
    """Browser-safe start result: no provider secrets."""

    session_id: str
    livekit_url: str
    livekit_client_token: str
    mode: str = "self-host"


class SessionStatusResponse(BaseModel):
    session_id: str
    status: str