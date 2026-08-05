"""Async-safe correlation context and transport metadata propagation."""

from __future__ import annotations

import re
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

CONTEXT_FIELDS = ("session_id", "request_id", "trace_id", "component")
TRANSPORT_FIELDS = {
    "x-session-id": "session_id",
    "x-request-id": "request_id",
    "x-trace-id": "trace_id",
    "x-component": "component",
}
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z", re.ASCII)
_CONTEXT_VARS: dict[str, ContextVar[str | None]] = {
    field: ContextVar(f"observability_{field}", default=None) for field in CONTEXT_FIELDS
}


@dataclass(frozen=True, slots=True)
class ContextTokens:
    """Tokens required to restore a previous correlation context."""

    values: tuple[tuple[str, Token[str | None]], ...]


def validate_identifier(field: str, value: object) -> str:
    """Validate one bounded, transport-safe correlation identifier."""
    if field not in _CONTEXT_VARS:
        raise ValueError(f"Unknown context field: {field!r}")
    if not isinstance(value, str) or not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {field}: expected 1-128 transport-safe ASCII characters")
    return value


def bind(**identifiers: object) -> ContextTokens:
    """Bind validated identifiers and return tokens for restoration."""
    validated = {field: validate_identifier(field, value) for field, value in identifiers.items()}
    return ContextTokens(
        tuple((field, _CONTEXT_VARS[field].set(value)) for field, value in validated.items())
    )


def reset(tokens: ContextTokens | None = None) -> None:
    """Restore tokenized values, or clear the complete current context."""
    if tokens is None:
        for variable in _CONTEXT_VARS.values():
            variable.set(None)
        return
    for field, token in reversed(tokens.values):
        _CONTEXT_VARS[field].reset(token)


def get(field: str, default: str | None = None) -> str | None:
    """Return one context value."""
    if field not in _CONTEXT_VARS:
        raise ValueError(f"Unknown context field: {field!r}")
    return _CONTEXT_VARS[field].get() or default


def get_all() -> dict[str, str]:
    """Return all currently bound identifiers."""
    return {
        field: value
        for field, variable in _CONTEXT_VARS.items()
        if (value := variable.get()) is not None
    }


@contextmanager
def scoped(**identifiers: object) -> Generator[None, None, None]:
    """Bind identifiers temporarily and always restore prior values."""
    tokens = bind(**identifiers)
    try:
        yield
    finally:
        reset(tokens)


def extract_from_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Extract and validate supported inbound transport metadata."""
    extracted: dict[str, str] = {}
    for raw_name, value in headers.items():
        if not isinstance(raw_name, str):
            raise ValueError("Transport metadata names must be strings")
        field = TRANSPORT_FIELDS.get(raw_name.lower())
        if field is None:
            continue
        if field in extracted:
            raise ValueError(f"Duplicate transport metadata for {field}")
        extracted[field] = validate_identifier(field, value)
    return extracted


def outbound_headers() -> dict[str, str]:
    """Return validated transport metadata for the current context."""
    current = get_all()
    return {
        header: validate_identifier(field, current[field])
        for header, field in TRANSPORT_FIELDS.items()
        if field in current
    }


@contextmanager
def scoped_from_headers(headers: Mapping[str, str]) -> Generator[None, None, None]:
    """Validate and bind inbound metadata for exactly one operation."""
    with scoped(**extract_from_headers(headers)):
        yield
