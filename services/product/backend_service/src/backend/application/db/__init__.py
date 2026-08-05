"""Canonical runtime persistence (OpenSpec 1.23).

Session-store contract + memory/Redis/Postgres adapters + raw runtime SQL
under ``db/sql/``. Legacy ``core.store`` / ``core.db`` remain shims.
"""

from __future__ import annotations

from .memory_session_store import InMemorySessionStore
from .redis_session_store import RedisSessionStore
from .session_store import SessionStore

__all__ = ["InMemorySessionStore", "RedisSessionStore", "SessionStore"]
