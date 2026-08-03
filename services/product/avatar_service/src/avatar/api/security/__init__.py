"""Security package for the avatar self-host service."""

from avatar.api.security.authentication import (
    AuthenticationError,
    authenticate_bearer,
    get_auth_subject,
    get_security_config,
)
from avatar.api.security.authorization import (
    AuthorizationError,
    require_scope,
    validate_scope,
)
from avatar.api.security.rate_limit import (
    ConcurrencyLimitError,
    ConcurrencyLimiter,
    GPUConcurrencyLimiter,
    RateLimitError,
    RateLimiter,
)
from avatar.api.security.config import SecurityConfig

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
