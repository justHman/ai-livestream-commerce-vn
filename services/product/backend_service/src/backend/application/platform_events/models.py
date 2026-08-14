"""Canonical platform event envelope for viewer ingress (OpenSpec 2.1).

One normalized ``PlatformEvent`` shape for every source platform (TikTok,
Shopee, Facebook, ...): provenance fields (``platform``, ``source_stream_id``)
are observed and stored, never branched on. ``event_id`` is the idempotency
key — bounded in length, matched as the exact string, unique per session.

Validation lives in Pydantic so the HTTP boundary rejects malformed events
with 422 before the ingestion service sees them; the service additionally
rejects structurally valid but unusable events (stale ``occurred_at``,
empty/oversized comment text) with typed reason codes.
"""

from __future__ import annotations

from typing import Literal, Optional, Union

from pydantic import BaseModel, Field, model_validator
from pydantic_core import PydanticCustomError

MAX_COMMENT_TEXT = 500
MAX_EVENTS_PER_REQUEST = 100
MAX_STALENESS_SEC = 24 * 60 * 60  # |now - occurred_at| > 24h is rejected

# Comment text is capped far below the HTTP body limit so oversized text is
# always a per-event validation error (413 via string_too_long), never a
# whole-batch body-limit rejection.
MAX_REQUEST_BODY_BYTES = 65_536

EventType = Literal["viewer.comment", "viewer.join", "viewer.follow", "viewer.like"]


class ViewerRef(BaseModel):
    """Stable viewer identity when the source platform provides one."""

    viewer_id: str = Field(min_length=1, max_length=128)
    display_name: Optional[str] = Field(default=None, max_length=128)
    avatar_url: Optional[str] = Field(default=None, max_length=2_048)


class CommentPayload(BaseModel):
    """Payload for ``viewer.comment`` — the only semantically reduced event."""

    text: str = Field(min_length=1, max_length=MAX_COMMENT_TEXT)


class CountPayload(BaseModel):
    """Payload for join/follow/like events: optional batch count."""

    count: Optional[int] = Field(default=None, ge=1, le=100_000)
    model_config = {"extra": "forbid"}


class PlatformEvent(BaseModel):
    """One normalized viewer event from any source platform."""

    event_id: str = Field(min_length=1, max_length=128)
    platform: str = Field(min_length=1, max_length=32)
    source_stream_id: str = Field(min_length=1, max_length=256)
    occurred_at: float
    type: EventType
    viewer: Optional[ViewerRef] = None
    # The payload is discriminated by ``type`` (smart union, left-to-right):
    # oversized comment text fails CommentPayload's max_length and falls back
    # to the plain dict, where the after-validator enforces the bound with a
    # string_too_long error (-> canonical 413 envelope, no body echo).
    payload: Union[CommentPayload, CountPayload, dict] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_payload_for_type(self) -> "PlatformEvent":
        """Discriminate payload by event type (comment -> CommentPayload).

        Oversized text raises a ``string_too_long`` pydantic error so the
        shared exception handler maps it to the canonical 413 envelope
        (``input_too_long``) without echoing the submitted text.
        """
        if self.type == "viewer.comment":
            raw_text = (
                self.payload.text
                if isinstance(self.payload, CommentPayload)
                else (self.payload.get("text") if isinstance(self.payload, dict) else None)
            )
            if not isinstance(raw_text, str):
                raise ValueError("comment events require payload.text")
            if len(raw_text) > MAX_COMMENT_TEXT:
                raise PydanticCustomError(
                    "string_too_long",
                    "String should have at most {max_length} characters",
                    {"max_length": MAX_COMMENT_TEXT},
                )
            if not raw_text.strip():
                raise ValueError("payload.text must not be empty")
        else:
            if isinstance(self.payload, CommentPayload):
                raise ValueError(f"{self.type} events do not carry comment text")
        return self


class EventsIn(BaseModel):
    """Bounded batch of canonical events (one-or-many share this schema)."""

    events: list[PlatformEvent] = Field(min_length=1, max_length=MAX_EVENTS_PER_REQUEST)
