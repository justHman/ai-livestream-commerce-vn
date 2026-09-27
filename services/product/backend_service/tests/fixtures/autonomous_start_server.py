"""Local-only real Runtime HTTP consumer for the API 007 integration test.

Run with backend .venv Python from the service directory. No real platform,
credentials or viewer receipt. Authoring/LLM/media use the existing 005 fixture.
"""

import asyncio
import sys
from pathlib import Path

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
root = Path(__file__).resolve().parents[5]
for service in ("backend", "llm", "tts", "avatar"):
    sys.path.insert(0, str(root / "services/product" / f"{service}_service" / "src"))
from unit import test_approved_speech_active as speech_tests  # noqa: E402
from unit.test_autonomous_start import identity, prepared  # noqa: E402


async def main():
    monkeypatch = pytest.MonkeyPatch()
    fixture = speech_tests.case_factory.__wrapped__(monkeypatch)
    factory = await anext(fixture)
    case = await prepared(factory)
    app = case.client._transport.app

    @app.get("/fixture")
    async def fixture_state():
        return {
            "identity": identity(case),
            "media": case.d.coordinator.opening_media(case.sid),
            "opening_count": sum(
                e["type"] == "coordinator.speak_started" and e.get("action") == "autonomous_opening"
                for e in case.events
            ),
        }

    try:
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=int(sys.argv[1]), lifespan="off", log_level="warning"
            )
        )
        await server.serve()
    finally:
        await fixture.aclose()
        monkeypatch.undo()


if __name__ == "__main__":
    asyncio.run(main())
