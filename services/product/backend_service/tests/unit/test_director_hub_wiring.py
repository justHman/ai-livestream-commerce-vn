"""The composition root must hand the container's ControlHub to the Director coordinator.

Without it ``coordinator._emit`` is a no-op in production: no ``coordinator.speak_finished`` ever
reaches the control WebSocket, so the API's auto-end after the closing unit never fires.
"""

import asyncio

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
