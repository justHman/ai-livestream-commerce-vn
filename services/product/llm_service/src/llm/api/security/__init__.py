"""Security package for the LLM self-host service."""

from llm.api.security.authentication import (
    AuthenticationError,
    authenticate_bearer,
    require_auth,
)
from llm.api.security.authorization import (
    AuthorizationError,
    require_scope,
    validate_scope,
)
from llm.api.security.rate_limit import (
    ConcurrencyLimitError,
    ConcurrencyLimiter,
    GPUConcurrencyLimiter,
    RateLimitError,
    RateLimiter,
)
from llm.api.security.config import SecurityConfig

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
    "require_auth",
    "require_scope",
    "validate_scope",
]