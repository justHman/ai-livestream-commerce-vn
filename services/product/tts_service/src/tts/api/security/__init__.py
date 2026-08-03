"""Security package for the TTS self-host service."""

from tts.api.security.authentication import (
    AuthenticationError,
    authenticate_bearer,
    get_auth_subject,
    get_security_config,
)
from tts.api.security.authorization import (
    AuthorizationError,
    require_scope,
    validate_scope,
)
from tts.api.security.rate_limit import (
    ConcurrencyLimitError,
    ConcurrencyLimiter,
    GPUConcurrencyLimiter,
    RateLimitError,
    RateLimiter,
)
from tts.api.security.config import SecurityConfig

__all__ = [
    "AuthenticationError",
    "AuthorizationError",
    "ConcurrencyLimitError",
    "ConcurrencyLimiter",
    "GPUConcurrencyLimiter",
    "RateLimitError",
    "RateLimiter",
    "SecurityConfig",
    "authenticate_bearer",
    "get_auth_subject",
    "get_security_config",
    "require_scope",
    "validate_scope",
]
