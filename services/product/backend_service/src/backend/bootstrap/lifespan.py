"""backend.bootstrap.lifespan — bounded process resource startup/shutdown.

Owns the full container lifecycle:
  startup:  connect Postgres / configured store in explicit dependency order;
            a bounded retry keeps the app bootable when the DB is briefly
            unavailable (matching ``core.server`` parity), while any other
            startup failure cleans every resource already initialized.
  shutdown: bounded, dependency-ordered cleanup — orchestrator/session
            cancellation first, then coordinator, publishers, render clients,
            then the database.  One stage failure or timeout cannot skip the
            remaining independent stages; logs identify stage + error type
            but never secrets or customer data.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .container import BootstrapContainer

logger = logging.getLogger(__name__)

_STARTUP_ATTEMPTS = 3
_STARTUP_RETRY_DELAYS = (0.1, 0.2)
_SHUTDOWN_TIMEOUT_SECONDS = 10.0
_WS_RATE_LIMIT_CLOSE_CODE = 1008


# -- Startup ---------------------------------------------------------


async def _connect_postgres(container: BootstrapContainer) -> None:
    """Connect + apply schema with bounded retries (parity with core.server).

    ``CancelledError`` propagates immediately; other failures log and retry
    up to ``_STARTUP_ATTEMPTS`` at ``_STARTUP_RETRY_DELAYS``; on exhaustion the
    store is closed and the error is logged (server remains bootable, and
    readiness reports the failure honestly).
    """
    pg = container.pg_store
    if pg is None or not getattr(pg, "enabled", False):
        return
    for attempt in range(_STARTUP_ATTEMPTS):
        try:
            connect = pg.connect
            if inspect.iscoroutinefunction(connect):
                await connect()
            else:
                await asyncio.to_thread(connect)
            apply = getattr(pg, "apply_schema", None)
            if apply is not None:
                if inspect.iscoroutinefunction(apply):
                    await apply()
                else:
                    await asyncio.to_thread(apply)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            close = getattr(pg, "close", None)
            if close is not None:
                try:
                    if inspect.iscoroutinefunction(close):
                        await close()
                    else:
                        await asyncio.to_thread(close)
                except asyncio.CancelledError:
                    raise
                except Exception as close_exc:
                    logger.warning(
                        "Bootstrap postgres cleanup failed after startup attempt=%s error_type=%s",
                        attempt + 1,
                        type(close_exc).__name__,
                    )
            if attempt == _STARTUP_ATTEMPTS - 1:
                logger.error(
                    "Bootstrap postgres startup failed after %s attempts error_type=%s",
                    _STARTUP_ATTEMPTS,
                    type(exc).__name__,
                )
                return
            delay = _STARTUP_RETRY_DELAYS[attempt]
            logger.warning(
                "Bootstrap postgres startup failed attempt=%s retry_in_seconds=%s",
                attempt + 1,
                delay,
            )
            await asyncio.sleep(delay)


# -- Shutdown --------------------------------------------------------


async def _call_cleanup(operation, *, async_method: bool = False) -> None:
    """Run one cleanup callable, tolerating sync/async boundaries."""
    if async_method:
        result = operation()
        if inspect.isawaitable(result):
            await result
        return
    result = await asyncio.to_thread(operation)
    if inspect.isawaitable(result):
        await result


async def _call_orchestrators(container: BootstrapContainer) -> None:
    for session_id in list(getattr(container, "orchestrators", {}) or {}):
        entry = container.orchestrators.get(session_id) or {}
        orchestrator = entry.get("orchestrator")
        if orchestrator is None:
            continue
        cancel = getattr(orchestrator, "cancel", None)
        if cancel is None:
            continue
        if inspect.iscoroutinefunction(cancel):
            await cancel(session_id)
        else:
            await asyncio.to_thread(cancel, session_id)
    container.orchestrators.clear()


async def _shutdown(container: BootstrapContainer) -> None:
    """Bounded, dependency-ordered process teardown over the container.

    Each stage runs under a per-stage timeout. A failure/timeout of one stage
    is logged (stage + error type only) and does not block survivors.
    Later stages already satisfied by the container are skipped explicitly
    without being treated as failures.
    """

    async def run_stage(name: str, operation) -> None:
        try:
            await asyncio.wait_for(operation(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.error(
                "Bootstrap shutdown stage timed out stage=%s timeout_seconds=%s",
                name,
                _SHUTDOWN_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.error(
                "Bootstrap shutdown stage failed stage=%s error_type=%s",
                name,
                type(exc).__name__,
            )

    async def stop_coordinator() -> None:
        coordinator = getattr(container, "coordinator", None)
        if coordinator is not None:
            stop_all = getattr(coordinator, "stop_all", None)
            if stop_all is not None:
                await _call_cleanup(stop_all, async_method=inspect.iscoroutinefunction(stop_all))

    async def stop_session_pipeline() -> None:
        """Cancel any active orchestrator tasks so no producer outlives."""
        await _call_orchestrators(container)

    async def stop_livekit() -> None:
        publishers = getattr(container, "livekit_publishers", None)
        if publishers is not None:
            stop_all = getattr(publishers, "stop_all", None)
            if stop_all is not None:
                await _call_cleanup(stop_all, async_method=inspect.iscoroutinefunction(stop_all))

    async def stop_backend() -> None:
        backend = getattr(container, "backend", None)
        if backend is not None:
            stop_all = getattr(backend, "stop_all", None)
            if stop_all is not None:
                await _call_cleanup(stop_all, async_method=inspect.iscoroutinefunction(stop_all))

    async def close_clients() -> None:
        """Close client resources with an explicit `close` method.

        Resources without a close method are handled explicitly (skipped),
        not swallowed as success.
        """
        backend = getattr(container, "backend", None)
        if backend is not None:
            close = getattr(backend, "close", None)
            if close is not None:
                await _call_cleanup(close, async_method=inspect.iscoroutinefunction(close))
        store = getattr(container, "store", None)
        if store is not None:
            close = getattr(store, "close", None)
            if close is not None:
                await _call_cleanup(close, async_method=inspect.iscoroutinefunction(close))

    async def close_postgres() -> None:
        pg = getattr(container, "pg_store", None)
        if pg is not None and getattr(pg, "enabled", False):
            close = getattr(pg, "close", None)
            if close is not None:
                await _call_cleanup(close, async_method=inspect.iscoroutinefunction(close))

    stages = (
        ("orchestrators", stop_session_pipeline),
        ("coordinator", stop_coordinator),
        ("livekit.stop_all", stop_livekit),
        ("render.stop_all", stop_backend),
        ("clients.close", close_clients),
        ("postgres", close_postgres),
    )
    for name, operation in stages:
        await run_stage(name, operation)


# -- Lifespan ---------------------------------------------------------


def build_lifespan(container: BootstrapContainer):
    """Return the asyncio lifespan contextmanager for an app + container."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await _connect_postgres(container)
        try:
            yield
        finally:
            await _shutdown(container)

    return lifespan
