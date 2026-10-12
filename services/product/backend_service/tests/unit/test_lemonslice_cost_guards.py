import time

import pytest

from backend.application.clients.avatar.lemonslice import LemonSliceError
from backend.application.render.engines_base import StartOptions
from .test_lemonslice_backend import make, win


@pytest.mark.parametrize("phase", ["preparing", "ready"])
def test_pre_live_keeps_existing_avatar_but_never_heals_or_rolls_over(phase):
    backend, room, rest = make(keepalive_s=0.01, rollover_after_s=60)
    sid = backend.start(StartOptions()).session_id
    try:
        backend.set_session_phase(sid, phase)
        backend._sessions[sid].started_at -= 120
        time.sleep(0.05)
        assert len(rest.starts) == 1
        assert any(event == "reset-idle-timeout" for _, event in rest.controls)
        room.remote_participants.clear()
        time.sleep(0.12)
        assert len(rest.starts) == 1
        backend.stream_audio(sid, win("first", 0, final=True))
        assert len(rest.starts) == 2
    finally:
        backend.stop_all()


def test_credit_exhausted_stops_all_subsequent_creation_and_redacts(caplog):
    backend, _, rest = make(status=402, keepalive_s=0)
    try:
        for _ in range(5):
            with pytest.raises(LemonSliceError, match="LEMONSLICE_CREDIT_EXHAUSTED"):
                backend.start(StartOptions())
        assert len(rest.starts) == 1
        assert "LemonSlice hết credit" in caplog.text
        assert "ls-secret" not in caplog.text
    finally:
        backend.stop_all()


def test_live_402_rollover_is_terminal_and_does_not_retry(caplog):
    backend, room, rest = make(keepalive_s=0, rollover_after_s=0.01)
    sid = backend.start(StartOptions()).session_id
    try:
        rest.status = 402
        room.remote_participants.clear()
        for n in range(3):
            with pytest.raises(LemonSliceError, match="LEMONSLICE_CREDIT_EXHAUSTED"):
                backend.stream_audio(sid, win(f"u{n}", 0, final=True))
        assert len(rest.starts) == 2
    finally:
        backend.stop_all()


@pytest.mark.parametrize("phase", ["live", "warming", "selling", "closing"])
def test_on_air_phase_keeps_silent_avatar_alive(phase):
    backend, _, rest = make(keepalive_s=0.01)
    sid = backend.start(StartOptions()).session_id
    try:
        backend.set_session_phase(sid, phase)
        time.sleep(0.05)
        assert any(event == "reset-idle-timeout" for _, event in rest.controls)
    finally:
        backend.stop_all()


def test_recent_speech_enables_keepalive_without_execution_phase():
    backend, _, rest = make(keepalive_s=0.01)
    sid = backend.start(StartOptions()).session_id
    try:
        backend.stream_audio(sid, win("speech", 0, final=True))
        time.sleep(0.03)
        assert any(event == "reset-idle-timeout" for _, event in rest.controls)
    finally:
        backend.stop_all()


def test_402_circuit_and_fixed_error_survive_accounting_cleanup_failure():
    class BrokenFinish:
        async def begin(self, survival_s):
            return "charge"

        async def finish(self, token):
            raise ConnectionError("redis unavailable")

        async def close(self):
            pass

    backend, _, rest = make(status=402, keepalive_s=0)
    backend._budget = BrokenFinish()
    try:
        for _ in range(2):
            with pytest.raises(LemonSliceError, match="LEMONSLICE_CREDIT_EXHAUSTED"):
                backend.start(StartOptions())
        assert len(rest.starts) == 1
    finally:
        backend.stop_all()


@pytest.mark.parametrize("phase", ["preparing", "ready"])
def test_keepalive_402_is_terminal_with_no_more_resets(phase):
    backend, _, rest = make(keepalive_s=0.01)
    sid = backend.start(StartOptions()).session_id
    try:
        rest.control_status = 402
        backend.set_session_phase(sid, phase)
        time.sleep(0.08)
        assert len(rest.controls) == 1
        assert backend.lifecycle_status(sid, 1800) == "LEMONSLICE_CREDIT_EXHAUSTED"
    finally:
        rest.control_status = 200
        backend.stop_all()


def test_interrupted_partial_speech_does_not_disable_live_keepalive_forever():
    backend, _, rest = make(keepalive_s=0.01)
    sid = backend.start(StartOptions()).session_id
    try:
        backend.set_session_phase(sid, "selling")
        backend.stream_audio(sid, win("partial", 0))
        backend.interrupt(sid)
        time.sleep(0.05)
        assert not backend._sessions[sid].speech_pending
        assert any(event == "reset-idle-timeout" for _, event in rest.controls)
    finally:
        backend.stop_all()


@pytest.mark.asyncio
async def test_session_http_error_contains_fixed_credit_marker():
    from fastapi import HTTPException, Request
    from types import SimpleNamespace
    from backend.api.v1.sessions import sessions_start
    from backend.api.v1.router import StartReq

    backend, _, _ = make(status=402, keepalive_s=0)
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(container=SimpleNamespace(backend=backend))
            ),
        }
    )
    try:
        with pytest.raises(HTTPException) as error:
            await sessions_start(StartReq(), request, None, None)
        assert error.value.status_code == 502
        assert error.value.detail == "LEMONSLICE_CREDIT_EXHAUSTED: LemonSlice hết credit"
        assert len(error.value.detail) < 256
    finally:
        backend.stop_all()


@pytest.mark.parametrize(
    "variable",
    ["RUNTIME_READY_TTL_S", "LEMONSLICE_DAILY_AVATAR_MINUTES", "LEMONSLICE_CREDIT_BACKOFF_S"],
)
@pytest.mark.parametrize("value", ["-1", "nan", "inf", "bad-secret"])
def test_cost_env_timers_fail_closed_without_echoing_values(monkeypatch, variable, value):
    from backend.config import AppConfig

    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=variable) as error:
        AppConfig.from_env()
    assert "bad-secret" not in str(error.value)
