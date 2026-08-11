"""Stable provider domain errors (Change T task 2.7).

Each error carries a hint for the API layer's HTTP mapping. Actual status
mapping happens at the API boundary in the cluster that wires the runtime.
"""

from __future__ import annotations


class ProviderError(RuntimeError):
    """Base class for provider/voice domain failures."""

    http_status: int = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CapabilityError(ProviderError):
    """Requested capability (style, cue, format) unsupported by the provider."""

    http_status = 400


class ProfileNotFoundError(ProviderError):
    """Requested voice profile does not exist for this tenant."""

    http_status = 404


class ProfileUnauthorizedError(ProviderError):
    """Profile exists but the tenant has no access — do not leak existence."""

    http_status = 403


class OverloadError(ProviderError):
    """Admission bound exceeded (global or per-session pending limit)."""

    http_status = 429


class DeadlineExceededError(ProviderError):
    """Request's deadline expired before it could be dispatched."""

    http_status = 408


class CancelledError(ProviderError):
    """Caller disconnected before dispatch; the pending request was removed."""

    http_status = 499


class ProviderUnavailableError(ProviderError):
    """Provider/model subsystem not ready (startup failure or teardown)."""

    http_status = 503


class ProviderInferenceError(ProviderError):
    """Provider inference failed; siblings resolve independently."""

    http_status = 502
