"""P0-FB-010c r2: stop_all waits for a late session's terminate retries (budget from config)."""

from __future__ import annotations

import time

import pytest

import backend.application.clients.avatar.lemonslice as mod
from backend.application.render.engines_base import StartOptions

from .test_lemonslice_races import bg, join, make

pytestmark = pytest.mark.timeout(60)


def test_shutdown_during_creation_lets_the_reaper_run_its_terminate_retry(monkeypatch):
    monkeypatch.setattr(mod, "_CREATE_HARD_BOUND_S", 1.0)  # old fixed drain bound = 1 + 5 s
    backend, room, rest = make(
        keepalive_s=0,
        request_timeout_s=3.0,
        control_deadline_s=9.0,
        terminate_attempts=2,
        terminate_path="/sessions/{session_id}/terminate",
    )
    terminates: list[float] = []

    def post(url, headers, body, timeout):
        if body.get("event") == "terminate":
            terminates.append(time.monotonic())
            if len(terminates) == 1:
                time.sleep(7.5)  # slower than the old fixed bound, inside control_deadline_s
                return 500, {}
        return rest(url, headers, body, timeout)

    object.__setattr__(backend, "_post", post)
    backend._async_post = None  # sync fake: control calls run in a worker thread
    release = rest.hold()
    t_start, _ = bg(backend.start, StartOptions())
    assert rest.parked.wait(5)
    t_stop, _ = bg(backend.stop_all)
    time.sleep(0.3)
    release.set()  # the late session lands while shutdown is in progress
    join(t_stop, timeout=50)
    join(t_start)
    assert len(terminates) == 2  # the configured retry ran before stop_all returned


def test_drain_budget_follows_the_configured_control_timers():
    backend, *_ = make(control_deadline_s=80.0, terminate_attempts=2, keepalive_s=0)
    need = mod._CREATE_HARD_BOUND_S + 2 * 80.0
    assert backend._drain_budget("reapers") > need
    assert backend._drain_budget("cleanups") > 2 * 80.0
    small, *_ = make(control_deadline_s=1.0, terminate_attempts=1, keepalive_s=0)
    assert small._drain_budget("cleanups") < backend._drain_budget("cleanups")
