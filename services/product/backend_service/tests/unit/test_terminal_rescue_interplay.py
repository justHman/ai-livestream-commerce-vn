"""P0-FB-019 x 016: the terminal record is written once per session end (stop), never by Hold / Interrupt / Resume."""

from __future__ import annotations

import pytest

from backend.api.v1.sessions import _stop_cancelled_session
from backend.application.execution_contract import RESCUE_SWITCH
from backend.application.terminal_outcomes import TerminalOutcomes

from .test_rescue_commands import live, send
from . import test_approved_speech_active as speech_tests
from .test_terminal_outcomes import FakePg

case_factory = speech_tests.case_factory
pytestmark = pytest.mark.timeout(30)


@pytest.fixture(autouse=True)
def _rescue(monkeypatch):
    monkeypatch.setenv(RESCUE_SWITCH, "1")


async def _enable(case):
    pg = FakePg()
    case.d.terminal_outcomes = TerminalOutcomes(pg)
    return pg


async def test_hold_resume_interrupt_never_write_a_terminal_record(case_factory):
    case = await live(case_factory)
    pg = await _enable(case)
    for command in ("hold", "resume", "interrupt"):
        await send(case, command)
    assert pg.calls == [] and pg.stored == {}


@pytest.mark.parametrize(
    ("command", "reason"), [("end", "normal_end"), ("emergency_end", "merchant_emergency_end")]
)
async def test_end_commands_alone_write_nothing_and_the_stop_writes_exactly_once(
    case_factory, command, reason
):
    case = await live(case_factory)
    pg = await _enable(case)
    await send(case, "hold")
    await send(case, command)
    assert pg.calls == []  # applying the command is not the session end
    await _stop_cancelled_session(case.d, case.sid)
    assert pg.calls == ["persist"] and len(pg.stored) == 1
    (record,) = pg.stored.values()
    assert record.reason_code == reason and record.business_outcome == "ENDED"
    assert record.command_ref.command_id == f"{command}-1"


def test_terminal_and_rescue_capabilities_compose():
    from backend.application.execution_contract import (
        TERMINAL_CAPABILITY,
        Capabilities,
        set_terminal_advertised,
    )

    set_terminal_advertised(True)
    try:
        both = Capabilities.for_session({"p0_rescue": True})
        plain = Capabilities.for_session({})
    finally:
        set_terminal_advertised(False)
    assert both.supports(TERMINAL_CAPABILITY, "command.end", "command.hold")
    assert plain.supports(TERMINAL_CAPABILITY) and not plain.supports("command.end")
    assert not Capabilities.for_session({"p0_rescue": True}).supports(TERMINAL_CAPABILITY)
