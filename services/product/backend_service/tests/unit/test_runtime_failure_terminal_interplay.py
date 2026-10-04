"""Accepted faults participate in real rescue/Stop/019 terminal precedence."""

import pytest

from backend.api.v1.sessions import _stop_cancelled_session
from backend.application.execution_contract import RESCUE_SWITCH
from backend.application.runtime_failures import FailureSettings, RuntimeFailures
from backend.application.terminal_outcomes import TerminalOutcomes
from . import test_approved_speech_active as speech_tests
from .test_rescue_commands import live, send
from .test_terminal_outcomes import FakePg

case_factory = speech_tests.case_factory
pytestmark = pytest.mark.timeout(30)


async def enable(case, monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")
    pg = FakePg()
    case.d.terminal_outcomes = TerminalOutcomes(pg)
    monitor = RuntimeFailures(case.d, FailureSettings(enabled=True))
    case.d.runtime_failures = monitor
    monitor.register(case.sid, await case.d.store.get(case.sid))
    await case.d.terminal_outcomes.register(case.sid, await case.d.store.get(case.sid))
    return monitor, pg


@pytest.mark.parametrize("command", [None, "emergency_end", "end"])
async def test_accepted_failure_wins_before_command_or_stop(case_factory, monkeypatch, command):
    case = await live(case_factory)
    monitor, pg = await enable(case, monkeypatch)
    monitor.fail(case.sid, RuntimeError("private provider detail"))
    if command:
        response = await send(case, command)
        assert response["outcome"]["status"] == "rejected"
    await _stop_cancelled_session(case.d, case.sid)
    (record,) = pg.stored.values()
    assert record.business_outcome == "FAILED"
    assert record.failure_class == "runtime_error"
    assert record.reason_code == "execution_failed"
    assert case.sid not in monitor.pending
    await monitor.sweep()
    assert len(pg.stored) == 1


@pytest.mark.parametrize("mode", ["error", "timeout"])
async def test_shutdown_defers_unsaved_fault_without_success(case_factory, monkeypatch, mode):
    import asyncio
    from backend.api.v1 import execution
    from backend.bootstrap.lifespan import _persist_terminal_on_shutdown

    case = await live(case_factory)
    monitor, pg = await enable(case, monkeypatch)
    await send(case, "emergency_end")
    monitor.fail(case.sid, RuntimeError())
    monitor.settings = FailureSettings(enabled=True, attempt_timeout=0.01)

    async def unavailable(*args):
        if mode == "timeout":
            await asyncio.Event().wait()
        raise ConnectionError()

    with monkeypatch.context() as patch:
        patch.setattr(execution, "_save", unavailable)
        await _persist_terminal_on_shutdown(case.d)
    assert pg.stored == {}
    assert case.sid in monitor.pending
    assert await case.d.store.get(case.sid) is not None
    await _persist_terminal_on_shutdown(case.d)
    assert next(iter(pg.stored.values())).business_outcome == "FAILED"


async def test_earlier_durable_end_is_not_rewritten(case_factory, monkeypatch):
    case = await live(case_factory)
    monitor, pg = await enable(case, monkeypatch)
    await send(case, "emergency_end")
    from backend.api.v1.terminal_guard import teardown_then_persist

    await teardown_then_persist(case.d, case.sid)
    monitor.fail(case.sid, RuntimeError())
    await _stop_cancelled_session(case.d, case.sid)
    (record,) = pg.stored.values()
    assert record.business_outcome == "ENDED"
    assert record.reason_code == "merchant_emergency_end"


async def test_failed_promotion_save_cannot_persist_success_or_delete_meta(
    case_factory, monkeypatch
):
    case = await live(case_factory)
    monitor, pg = await enable(case, monkeypatch)
    monitor.fail(case.sid, RuntimeError())
    from backend.api.v1 import execution

    async def unavailable(*args):
        raise ConnectionError("private datastore detail")

    with monkeypatch.context() as patch:
        patch.setattr(execution, "_save", unavailable)
        with pytest.raises(ConnectionError):
            await _stop_cancelled_session(case.d, case.sid)
    assert pg.stored == {}
    assert await case.d.store.get(case.sid) is not None
    assert case.sid in monitor.pending
    await _stop_cancelled_session(case.d, case.sid)
    assert next(iter(pg.stored.values())).business_outcome == "FAILED"


async def test_shutdown_promotes_accepted_fault_before_terminal_record(case_factory, monkeypatch):
    case = await live(case_factory)
    monitor, pg = await enable(case, monkeypatch)
    monitor.fail(case.sid, RuntimeError())
    from backend.bootstrap.lifespan import _persist_terminal_on_shutdown

    await _persist_terminal_on_shutdown(case.d)
    (record,) = pg.stored.values()
    assert record.business_outcome == "FAILED"
    assert record.failure_class == "runtime_error"
