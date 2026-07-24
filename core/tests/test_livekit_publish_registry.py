"""Unit tests for per-session LiveKit publisher lifecycle."""

from __future__ import annotations

import asyncio

import pytest

from core import server
from core.api import v1
from core.config import AppConfig
from core.livekit_publish import LiveKitPublisherRegistry
from core.render.mock import MockRenderBackend
from core.render.windows import AudioWindow
from core.store import InMemorySessionStore


class _FakePublisher:
    def __init__(self, session_id: str, *, publish_gate: asyncio.Event | None = None) -> None:
        self.session_id = session_id
        self.publish_gate = publish_gate
        self.publish_started = asyncio.Event()
        self.published: list[tuple[bytes, int, int]] = []
        self.stop_calls = 0

    async def publish_pcm(self, pcm: bytes, *, sample_rate: int, num_channels: int) -> None:
        self.publish_started.set()
        if self.publish_gate is not None:
            await self.publish_gate.wait()
        self.published.append((pcm, sample_rate, num_channels))

    async def stop(self) -> None:
        self.stop_calls += 1


class _Factory:
    def __init__(self, *, publish_gate: asyncio.Event | None = None) -> None:
        self.publish_gate = publish_gate
        self.publishers: list[_FakePublisher] = []

    def __call__(self, session_id: str) -> _FakePublisher:
        publisher = _FakePublisher(session_id, publish_gate=self.publish_gate)
        self.publishers.append(publisher)
        return publisher


def _window(session_id: str, *, sample_rate: int = 16_000) -> AudioWindow:
    return AudioWindow(
        session_id=session_id,
        utterance_id="utterance",
        seq=0,
        sample_rate=sample_rate,
        duration_ms=20,
        is_final=True,
        pcm=b"\x01\x00" * 320,
    )


@pytest.mark.asyncio
async def test_registry_reuses_one_publisher_and_forwards_native_rate() -> None:
    factory = _Factory()
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: True)
    registry.activate("session")

    await registry.publish(_window("session"))
    await registry.publish(_window("session"))

    assert len(factory.publishers) == 1
    assert factory.publishers[0].published == [
        (b"\x01\x00" * 320, 16_000, 1),
        (b"\x01\x00" * 320, 16_000, 1),
    ]


@pytest.mark.asyncio
async def test_registry_does_not_create_publisher_when_disabled() -> None:
    factory = _Factory()
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: False)
    registry.activate("session")

    await registry.publish(_window("session"))

    assert factory.publishers == []


@pytest.mark.asyncio
async def test_stop_prevents_a_late_window_from_recreating_publisher() -> None:
    gate = asyncio.Event()
    factory = _Factory(publish_gate=gate)
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: True)
    registry.activate("session")
    publish_task = asyncio.create_task(registry.publish(_window("session")))
    while not factory.publishers:
        await asyncio.sleep(0)
    await factory.publishers[0].publish_started.wait()
    stop_task = asyncio.create_task(registry.stop("session"))
    gate.set()
    await publish_task
    await stop_task
    await registry.publish(_window("session"))

    assert len(factory.publishers) == 1
    assert factory.publishers[0].stop_calls == 1
    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_reactivate_after_stop_creates_a_fresh_publisher() -> None:
    factory = _Factory()
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))
    await registry.stop("session")
    registry.activate("session")
    await registry.publish(_window("session"))

    assert len(factory.publishers) == 2
    assert factory.publishers[0].stop_calls == 1
    assert factory.publishers[1].published[0][1:] == (16_000, 1)


@pytest.mark.asyncio
async def test_stop_drops_a_publish_already_waiting_for_its_publisher_lock() -> None:
    factory = _Factory()
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))
    entry = registry._entries["session"]

    await entry.lock.acquire()
    publish_task = asyncio.create_task(registry.publish(_window("session")))
    await asyncio.sleep(0)
    stop_task = asyncio.create_task(registry.stop("session"))
    await asyncio.sleep(0)
    entry.lock.release()
    await asyncio.gather(publish_task, stop_task)

    assert len(factory.publishers[0].published) == 1
    assert factory.publishers[0].stop_calls == 1
    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_streaming_session_forwards_pcm_and_stops_its_publisher() -> None:
    factory = _Factory()
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: True)
    config = AppConfig(render_backend="mock", app_env="dev")
    deps = v1.V1Deps(
        backend=MockRenderBackend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        config=config,
        livekit_publishers=registry,
    )
    v1.init_deps(deps)

    started = await v1.lite_start(v1.StartReq())
    session_id = started["session_id"]
    await v1.lite_say(v1.SayReq(session_id=session_id, text="Xin chào"))
    await v1.lite_stop(v1.SessionReq(session_id=session_id))

    assert factory.publishers[0].published[0][1:] == (24_000, 1)
    assert factory.publishers[0].stop_calls == 1
    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_concurrent_stops_disconnect_one_publisher_once() -> None:
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()

    class Publisher:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def publish_pcm(self, *args, **kwargs) -> None:
            return None

        async def stop(self) -> None:
            self.stop_calls += 1
            stop_started.set()
            await stop_release.wait()

    publisher = Publisher()
    registry = LiveKitPublisherRegistry(lambda _: publisher, enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))

    first = asyncio.create_task(registry.stop("session"))
    await stop_started.wait()
    second = asyncio.create_task(registry.stop("session"))
    await asyncio.sleep(0)
    stop_release.set()
    await asyncio.gather(first, second)

    assert publisher.stop_calls == 1
    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_stop_failure_removes_inactive_publisher_entry() -> None:
    class Publisher:
        async def publish_pcm(self, *args, **kwargs) -> None:
            return None

        async def stop(self) -> None:
            raise RuntimeError("disconnect failed")

    registry = LiveKitPublisherRegistry(lambda _: Publisher(), enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))

    with pytest.raises(RuntimeError, match="disconnect failed"):
        await registry.stop("session")

    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_stop_all_stops_every_publisher() -> None:
    factory = _Factory()
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: True)
    registry.activate("one")
    registry.activate("two")

    await registry.publish(_window("one"))
    await registry.publish(_window("two"))
    await registry.stop_all()

    assert [publisher.stop_calls for publisher in factory.publishers] == [1, 1]
    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_stop_all_concurrently_runs_once_per_publisher() -> None:
    stop_started = asyncio.Event()
    stop_release = asyncio.Event()

    class Publisher:
        def __init__(self) -> None:
            self.stop_calls = 0

        async def publish_pcm(self, *args, **kwargs) -> None:
            return None

        async def stop(self) -> None:
            self.stop_calls += 1
            stop_started.set()
            await stop_release.wait()

    publisher = Publisher()
    registry = LiveKitPublisherRegistry(lambda _: publisher, enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))

    first = asyncio.create_task(registry.stop_all())
    await stop_started.wait()
    second = asyncio.create_task(registry.stop_all())
    await asyncio.sleep(0)
    stop_release.set()
    await asyncio.gather(first, second)

    assert publisher.stop_calls == 1
    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_stop_all_removes_a_publisher_that_fails_to_stop() -> None:
    class Publisher:
        async def publish_pcm(self, *args, **kwargs) -> None:
            return None

        async def stop(self) -> None:
            raise RuntimeError("disconnect failed")

    registry = LiveKitPublisherRegistry(lambda _: Publisher(), enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))

    await registry.stop_all()

    assert registry.session_ids == ()


@pytest.mark.asyncio
async def test_server_shutdown_stops_livekit_publishers_after_orchestrators() -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pcm(self, *args, **kwargs) -> None:
            return None

        async def stop(self) -> None:
            events.append("publisher")

    registry = LiveKitPublisherRegistry(lambda _: Publisher(), enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))

    class Orchestrator:
        async def cancel(self, session_id: str) -> None:
            events.append(f"orchestrator:{session_id}")

    class Backend(MockRenderBackend):
        def stop_all(self) -> None:
            events.append("backend")

    config = AppConfig(render_backend="mock", app_env="dev")
    deps = v1.V1Deps(
        backend=Backend(),
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        config=config,
        orchestrators={"session": {"orchestrator": Orchestrator()}},
        livekit_publishers=registry,
    )

    await server._shutdown(deps, None)

    assert events == ["orchestrator:session", "publisher", "backend"]


@pytest.mark.asyncio
async def test_stop_cancels_active_orchestrator_before_backend_and_publisher() -> None:
    events: list[str] = []

    class Publisher:
        async def publish_pcm(self, *args, **kwargs) -> None:
            return None

        async def stop(self) -> None:
            events.append("publisher")

    class Orchestrator:
        async def cancel(self, session_id: str) -> None:
            events.append(f"orchestrator:{session_id}")

    class Backend:
        def stop(self, session_id: str) -> None:
            events.append("backend")

    registry = LiveKitPublisherRegistry(lambda _: Publisher(), enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))
    config = AppConfig(render_backend="mock", app_env="dev")
    v1.init_deps(
        v1.V1Deps(
            backend=Backend(),
            store=InMemorySessionStore(),
            hub=v1.ControlHub(),
            config=config,
            orchestrators={"session": {"orchestrator": Orchestrator()}},
            livekit_publishers=registry,
        )
    )

    await v1.lite_stop(v1.SessionReq(session_id="session"))

    assert events == ["orchestrator:session", "backend", "publisher"]


@pytest.mark.asyncio
async def test_stop_removes_session_publisher_only_after_backend_stop_succeeds() -> None:
    factory = _Factory()
    registry = LiveKitPublisherRegistry(factory, enabled_predicate=lambda: True)
    registry.activate("session")
    await registry.publish(_window("session"))
    config = AppConfig(render_backend="mock", app_env="dev")
    backend = MockRenderBackend()
    backend._sessions["session"] = object()
    deps = v1.V1Deps(
        backend=backend,
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        config=config,
        livekit_publishers=registry,
    )
    v1.init_deps(deps)

    with pytest.raises(v1.HTTPException, match="unknown session_id"):
        await v1.lite_stop(v1.SessionReq(session_id="missing"))

    assert factory.publishers[0].stop_calls == 0
    assert registry.session_ids == ("session",)

    await v1.lite_stop(v1.SessionReq(session_id="session"))

    assert factory.publishers[0].stop_calls == 1
    assert registry.session_ids == ()
