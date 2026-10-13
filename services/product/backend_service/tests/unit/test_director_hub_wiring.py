"""The composition root must hand the container's ControlHub to the Director coordinator.

Without it ``coordinator._emit`` is a no-op in production: no ``coordinator.speak_finished`` ever
reaches the control WebSocket, so the API's auto-end after the closing unit never fires.
"""

import asyncio
import time

from backend.application.director import coordinator as coordinator_module
from backend.bootstrap.app_factory import _build_container
from backend.config import AppConfig


def test_container_coordinator_emits_through_the_container_hub(monkeypatch):
    for key, value in {
        "RENDER_BACKEND": "mock",
        "LLM_ENGINE": "none",
        "TTS_ENGINE": "tone",
        "DIRECTOR_ENABLED": "1",
        "APP_ENV": "dev",
    }.items():
        monkeypatch.setenv(key, value)
    container = _build_container(AppConfig.from_env(), None)
    assert container.coordinator is not None
    sent = []

    class Ws:
        async def send_json(self, event):
            sent.append(event)

    container.hub._conns["s1"] = Ws()
    asyncio.run(container.coordinator._emit("s1", {"type": "coordinator.speak_finished"}))
    assert sent == [{"type": "coordinator.speak_finished"}]


def _coordinator(monkeypatch):
    for key, value in {
        "RENDER_BACKEND": "mock",
        "LLM_ENGINE": "none",
        "TTS_ENGINE": "tone",
        "DIRECTOR_ENABLED": "1",
        "APP_ENV": "dev",
    }.items():
        monkeypatch.setenv(key, value)
    return _build_container(AppConfig.from_env(), None).coordinator


class _Hub:
    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.events = []

    async def emit(self, session_id, event):
        await self.behaviour(self, event)


async def _record(hub, event):
    hub.events.append(event)


async def _block_forever(hub, event):
    await asyncio.Event().wait()


async def _raise(hub, event):
    raise RuntimeError("subscriber exploded with payload-secret")


def test_a_stuck_subscriber_cannot_stall_the_coordinator(monkeypatch, caplog):
    monkeypatch.setattr(coordinator_module, "HUB_EMIT_TIMEOUT_SEC", 0.05)
    coordinator = _coordinator(monkeypatch)
    stuck, healthy = _Hub(_block_forever), _Hub(_record)

    async def scenario():
        coordinator._hub = stuck
        started = time.monotonic()
        await coordinator._emit("s1", {"type": "coordinator.speak_started", "text": "private"})
        elapsed = time.monotonic() - started
        coordinator._hub = healthy  # the next emit still works
        await coordinator._emit("s1", {"type": "coordinator.speak_finished"})
        return elapsed

    with caplog.at_level("WARNING"):
        elapsed = asyncio.run(scenario())
    assert elapsed < 1.0
    assert healthy.events == [{"type": "coordinator.speak_finished"}]
    assert "session=s1" in caplog.text and "coordinator.speak_started" in caplog.text
    assert "private" not in caplog.text  # event type only, never the payload


def test_a_subscriber_error_is_swallowed_with_a_warning(monkeypatch, caplog):
    coordinator = _coordinator(monkeypatch)
    coordinator._hub = _Hub(_raise)
    with caplog.at_level("WARNING"):
        asyncio.run(coordinator._emit("s1", {"type": "coordinator.speak_finished"}))
    assert "hub emit failed session=s1 event=coordinator.speak_finished" in caplog.text
    assert "payload-secret" not in caplog.text  # the exception text is not logged either


def test_cancelling_the_turn_is_not_swallowed(monkeypatch):
    coordinator = _coordinator(monkeypatch)
    coordinator._hub = _Hub(_block_forever)

    async def scenario():
        task = asyncio.ensure_future(coordinator._emit("s1", {"type": "x"}))
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            return True
        return False

    assert asyncio.run(scenario()) is True
