"""backend.application.platform_events — canonical multi-platform viewer ingress."""

from .ingestion import EventStatus, PlatformEventIngestionService
from .models import (
    MAX_COMMENT_TEXT,
    MAX_EVENTS_PER_REQUEST,
    CommentPayload,
    CountPayload,
    EventsIn,
    PlatformEvent,
    ViewerRef,
)

__all__ = [
    "CommentPayload",
    "CountPayload",
    "EventStatus",
    "EventsIn",
    "MAX_COMMENT_TEXT",
    "MAX_EVENTS_PER_REQUEST",
    "PlatformEvent",
    "PlatformEventIngestionService",
    "ViewerRef",
]
