"""Director-level errors shared with the platform-event ingress."""

from __future__ import annotations


class CoordinatorUnavailable(KeyError):
    """The coordinator has no live session to take a comment (never started or torn down).

    A ``KeyError`` subclass so existing ``except KeyError`` callers keep
    working; the per-event ingest guard isolates ONLY this class.
    """
