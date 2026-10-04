"""backend.bootstrap.lifespan — bounded process resource startup/shutdown.

Owns the full container lifecycle:
  startup:  connect Postgres / configured store in explicit dependency order;
            a bounded retry keeps the app bootable when the DB is briefly
            unavailable, while any other
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


def _start_reducer_loop(container: BootstrapContainer) -> None:
    """Start the single reducer-loop task that serves all sessions (P0-01).

    Only when the composition wired a FastReducer (``DIRECTOR_ENABLED=1``).
    The task is stored on the container so ``_shutdown`` can cancel it.
    """
    reducer = getattr(container, "reducer", None)
    if reducer is None:
        return
    container.reducer_loop_task = asyncio.create_task(reducer.run_loop(), name="reducer-loop")


async def _stop_reducer_loop(container: BootstrapContainer) -> None:
    """Cancel and await the reducer-loop task; safe when absent/done.

    The cancelled task reference stays on the container so shutdown is
    observable (``reducer_loop_task.done()`` is True after exit).
    """
    task = getattr(container, "reducer_loop_task", None)
    if task is None:
        return
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _connect_postgres(container: BootstrapContainer) -> None:
    """Connect + apply schema with bounded retries.

    ``CancelledError`` propagates immediately; other failures log and retry
    up to ``_STARTUP_ATTEMPTS`` at ``_STARTUP_RETRY_DELAYS``; on exhaustion the
    store is closed. In production the final failure is re-raised (fail-fast —
    the app must not boot with an unreachable DB); otherwise the error is
    logged and the server remains bootable (readiness reports it honestly).
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
                if container.config.is_production:
                    raise exc
                return
            delay = _STARTUP_RETRY_DELAYS[attempt]
            logger.warning(
                "Bootstrap postgres startup failed attempt=%s retry_in_seconds=%s",
                attempt + 1,
                delay,
            )
            await asyncio.sleep(delay)


async def _start_terminal_outcomes(container: BootstrapContainer) -> None:
    """P0-FB-019 (DISABLED slice): wire the durable terminal record and outbox.

    Inert unless TERMINAL_OUTCOMES_ENABLED is set AND a connected Postgres store,
    an https (or loopback) callback URL and a secret exist. Only then is the
    ``execution.terminal.v1`` capability advertised; any failure leaves it absent
    so the API keeps the legacy path.
    """
    from backend.application.execution_contract import set_terminal_advertised
    from backend.application.terminal_outcomes import (
        TerminalOutbox,
        TerminalOutcomes,
        TerminalSettings,
    )

    settings = TerminalSettings.from_env()
    if not settings.enabled:
        return
    pg = container.pg_store
    if not settings.configured or pg is None or not getattr(pg, "enabled", False):
        logger.error("Terminal outcomes enabled but not configured; capability stays absent")
        return
    try:
        await pg.apply_terminal_schema()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Terminal outcomes schema failed error_type=%s; capability stays absent",
            type(exc).__name__,
        )
        return
    container.terminal_outcomes = TerminalOutcomes(pg)
    container.terminal_outbox_task = asyncio.create_task(
        TerminalOutbox(pg, settings).run_loop(), name="terminal-outbox"
    )
    set_terminal_advertised(True)


async def _start_usage_evidence(container: BootstrapContainer) -> None:
    """P0-FB-017: wire the durable signed usage-evidence outbox and background sender.

    Fail closed: inert unless USAGE_EVIDENCE_ENABLED AND a connected Postgres store, the
    exact receiver URL and a secret exist. Only then is ``usage.evidence.v1`` advertised;
    any failure leaves it absent and no row is ever written.
    """
    from backend.application.execution_contract import set_usage_evidence_capabilities
    from backend.application.usage_evidence import UsageEvidence, UsageSender
    from backend.application.usage_evidence.outbox import UsageOutbox
    from backend.application.usage_evidence.settings import UsageEvidenceSettings

    settings = UsageEvidenceSettings.from_env()
    if not settings.enabled:
        return
    pg = container.pg_store
    if not settings.configured or pg is None or not getattr(pg, "enabled", False):
        logger.error("Usage evidence enabled but not configured; capability stays absent")
        return
    try:
        await pg.apply_usage_evidence_schema()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "Usage evidence schema failed error_type=%s; capability stays absent",
            type(exc).__name__,
        )
        return
    outbox = UsageOutbox(pg, producer_id=settings.producer_id)
    service = UsageEvidence(outbox, settings)
    from backend.api.v1.execution import _locked

    sender = UsageSender(
        outbox,
        settings,
        session_store=container.store,
        session_lock=lambda session_id: _locked(container.store, session_id),
        cogs=service.cogs,
        unstaged_sessions=service.unstaged_sessions,
    )
    container.usage_evidence = service
    container.usage_sender = sender
    container.usage_evidence_task = asyncio.create_task(sender.run_loop(), name="usage-evidence")
    set_usage_evidence_capabilities(settings.capabilities())


async def _stop_usage_evidence(container: BootstrapContainer) -> None:
    """Stop after the coordinator and before Postgres closes, with a bounded final flush."""
    from backend.application.execution_contract import set_usage_evidence_capabilities

    set_usage_evidence_capabilities(())
    task = getattr(container, "usage_evidence_task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    sender = getattr(container, "usage_sender", None)
    if sender is not None:
        service = getattr(container, "usage_evidence", None)
        timeout = service.settings.flush_timeout if service is not None else 5.0
        try:
            await sender.flush(timeout)
        finally:
            await sender.close()
    # The service object stays: a request during shutdown still stages durably, and
    # the rows are delivered by the next process.


async def _persist_terminal_on_shutdown(container: BootstrapContainer) -> None:
    """P0-FB-019: before any component stops, give every unterminated P0 execution a
    durable record (or an explicit audited deferral). No-op while disabled."""
    terminal = getattr(container, "terminal_outcomes", None)
    if terminal is not None:
        extra = set(getattr(container, "orchestrators", {}) or {})
        publishers = getattr(container, "livekit_publishers", None)
        extra.update(getattr(publishers, "session_ids", ()) or ())
        await terminal.persist_active_on_shutdown(container.store, tuple(extra))


async def _stop_terminal_outcomes(container: BootstrapContainer) -> None:
    from backend.application.execution_contract import set_terminal_advertised

    set_terminal_advertised(False)
    task = getattr(container, "terminal_outbox_task", None)
    if task is None:
        return
    if not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _start_budget_lease(container: BootstrapContainer) -> None:
    """P0-FB-018 S6: expiry watcher, only when LIVE_CREDIT_LEASE_ENFORCEMENT is on.

    Wiring and advertising are one step: ``budget.lease.v1`` appears only once the
    watcher task exists, and the route answers 409 until then.
    """
    from backend.application import budget_lease

    settings = budget_lease.LeaseSettings.from_env()
    if not settings.enabled:
        return
    enforcer = budget_lease.BudgetLeaseEnforcer(container, settings)
    container.budget_lease_enforcer = enforcer
    await enforcer.rehydrate()  # persisted expiries gate admission before any traffic
    container.budget_lease_task = asyncio.create_task(enforcer.run_loop(), name="budget-lease")
    budget_lease.set_active(True)


async def _stop_budget_lease(container: BootstrapContainer) -> None:
    from backend.application import budget_lease

    budget_lease.set_active(False)
    task = getattr(container, "budget_lease_task", None)
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def _start_runtime_failures(container: BootstrapContainer) -> None:
    from backend.application.runtime_failures import FailureSettings, RuntimeFailures

    settings = FailureSettings.from_env()
    if not settings.enabled:
        return
    if container.coordinator is None or container.approved_speech is None:
        raise RuntimeError("runtime failure detection requires coordinator and speech gate")
    if container.terminal_outcomes is None:
        raise RuntimeError("runtime failure detection requires durable terminal outcomes")
    service = RuntimeFailures(container, settings)
    container.runtime_failures = service
    container.coordinator.runtime_failures = service
    container.runtime_failures_task = asyncio.create_task(
        service.run_loop(), name="runtime-failures"
    )


async def _stop_runtime_failures(container: BootstrapContainer) -> None:
    service = getattr(container, "runtime_failures", None)
    if service is None:
        return
    container.coordinator.runtime_failures = None
    task = container.runtime_failures_task
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    for flush in tuple(service.flush_tasks):
        flush.cancel()
    await asyncio.gather(*tuple(service.flush_tasks), return_exceptions=True)


async def _connect_authoring(container: BootstrapContainer) -> None:
    """Connect the Change B authoring repositories with bounded retries.

    The script_* tables are owned by ``_connect_postgres``'s apply_schema, so
    this stage only establishes the authoring pool. When the composition wired
    no service (no DATABASE_URL) the authoring surface stays 501. On retry
    exhaustion the final failure is re-raised in production (fail-fast);
    otherwise failures log and the server remains bootable, mirroring
    ``_connect_postgres``.
    """
    service = getattr(container, "script_authoring_service", None)
    if service is None:
        return
    repos = getattr(service, "_repos", None)
    if repos is None or not getattr(repos, "enabled", False):
        return
    for attempt in range(_STARTUP_ATTEMPTS):
        try:
            connect = repos.connect
            if inspect.iscoroutinefunction(connect):
                await connect()
            else:
                await asyncio.to_thread(connect)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            close = getattr(repos, "close", None)
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
                        "Bootstrap script-authoring cleanup failed after startup attempt=%s error_type=%s",
                        attempt + 1,
                        type(close_exc).__name__,
                    )
            if attempt == _STARTUP_ATTEMPTS - 1:
                logger.error(
                    "Bootstrap script-authoring startup failed after %s attempts error_type=%s",
                    _STARTUP_ATTEMPTS,
                    type(exc).__name__,
                )
                if container.config.is_production:
                    raise exc
                return
            delay = _STARTUP_RETRY_DELAYS[attempt]
            logger.warning(
                "Bootstrap script-authoring startup failed attempt=%s retry_in_seconds=%s",
                attempt + 1,
                delay,
            )
            await asyncio.sleep(delay)


async def _recover_authoring(container: BootstrapContainer) -> None:
    """Resume durable authoring jobs/batches left by a previous process (HIGH-B).

    Runs AFTER ``_connect_authoring`` so the repository pool is live. In prod a
    recovery failure is fail-fast (matches the other startup gates); in dev the
    error is logged and the app stays bootable — the durable job remains
    recoverable on the next restart.
    """
    service = getattr(container, "script_authoring_service", None)
    if service is None:
        return
    recover = getattr(service, "recover_pending", None)
    if recover is None:
        return
    try:
        await recover()
    except Exception as exc:
        if container.config.is_production:
            raise
        logger.warning(
            "Bootstrap authoring recovery failed type=%s; jobs stay recoverable",
            type(exc).__name__,
        )


# -- Shutdown --------------------------------------------------------


async def _call_cleanup(operation, *, async_method: bool = False):
    """Run one cleanup callable, tolerating sync/async boundaries.

    Returns the callable's result: a teardown that hands back work to audit
    (coordinator ``stop_all`` -> delivery fences) must not lose it here.
    """
    if async_method:
        result = operation()
        if inspect.isawaitable(result):
            return await result
        return result
    result = await asyncio.to_thread(operation)
    if inspect.isawaitable(result):
        return await result
    return result


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
        """Stop every session, then audit the routed work they were holding.

        A process shutdown has no per-session ``/stop`` route to reconcile on,
        so it takes the fences ``stop_all`` returns and reconciles them here —
        the same "caller passes the fence to reconcile_session" contract the
        route uses. Without this, routed comments die with the queues and no
        audit row is ever written (P0-FB-013 silent loss).
        """
        coordinator = getattr(container, "coordinator", None)
        if coordinator is None:
            return
        stop_all = getattr(coordinator, "stop_all", None)
        if stop_all is None:
            return
        fences = await _call_cleanup(stop_all, async_method=inspect.iscoroutinefunction(stop_all))
        ingestion = getattr(container, "event_ingestion", None)
        if ingestion is None:
            return
        for session_id, attach_seq in (fences or {}).items():
            try:
                reconciled = await ingestion.reconcile_session(session_id, attach_seq=attach_seq)
            except Exception as exc:
                logger.error(
                    "Shutdown reconciliation failed session=%s error_type=%s",
                    session_id,
                    type(exc).__name__,
                )
                continue
            if reconciled:
                logger.info(
                    "shutdown reconciled non-deliverable events session=%s count=%d",
                    session_id,
                    len(reconciled),
                )

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

    async def close_authoring() -> None:
        service = getattr(container, "script_authoring_service", None)
        if service is None:
            return
        repos = getattr(service, "_repos", None)
        if repos is None or not getattr(repos, "enabled", False):
            return
        close = getattr(repos, "close", None)
        if close is not None:
            await _call_cleanup(close, async_method=inspect.iscoroutinefunction(close))

    async def drain_authoring() -> None:
        """Drain Script Authoring background jobs before any pool closes.

        HIGH-1: MUST run before ``close_authoring``. Awaited directly (not
        under the per-stage timeout) — cutting the drain short would leave a
        still-running job to race ``repos.close()``, the exact hang it
        prevents. The drain itself is bounded (configurable graceful window,
        then cancel + await stragglers).
        """
        service = getattr(container, "script_authoring_service", None)
        if service is None:
            return
        drain = getattr(service, "drain", None)
        if drain is not None:
            await _call_cleanup(drain, async_method=inspect.iscoroutinefunction(drain))

    # Drain Script Authoring background jobs BEFORE the authoring/postgres
    # close stages so no owned task races the pool close (HIGH-1).
    await drain_authoring()
    stages = (
        ("runtime.failures", lambda: _stop_runtime_failures(container)),
        # First: a stopping process must not turn its own teardown into control_lost.
        ("budget.lease", lambda: _stop_budget_lease(container)),
        ("terminal.shutdown", lambda: _persist_terminal_on_shutdown(container)),
        ("orchestrators", stop_session_pipeline),
        ("coordinator", stop_coordinator),
        ("reducer", lambda: _stop_reducer_loop(container)),
        ("terminal.outbox", lambda: _stop_terminal_outcomes(container)),
        ("usage.evidence", lambda: _stop_usage_evidence(container)),
        ("livekit.stop_all", stop_livekit),
        ("render.stop_all", stop_backend),
        ("clients.close", close_clients),
        ("authoring", close_authoring),
        ("postgres", close_postgres),
    )
    for name, operation in stages:
        await run_stage(name, operation)


# -- Lifespan ---------------------------------------------------------


def build_lifespan(container: BootstrapContainer):
    """Return the asyncio lifespan contextmanager for an app + container."""

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            await _connect_postgres(container)
            await _connect_authoring(container)
            await _recover_authoring(container)
            await _start_terminal_outcomes(container)
            await _start_usage_evidence(container)
            _start_reducer_loop(container)
            await _start_budget_lease(container)
            await _start_runtime_failures(container)
        except Exception:
            # Production startup is fail-fast: tear down any partially
            # initialized resource before the boot error propagates.
            await _shutdown(container)
            raise
        try:
            yield
        finally:
            await _shutdown(container)

    return lifespan
