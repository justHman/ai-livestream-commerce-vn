"""LiveAvatar playback acknowledgement contracts."""

from __future__ import annotations

import json
import threading

import pytest
import websocket

from providers.liveavatar_cloud.service.lite_agent import LiteAudioAgent


def test_stream_pcm_raises_when_playback_end_is_not_confirmed(monkeypatch) -> None:
    agent = LiteAudioAgent("ws://unused")
    agent._ws = object()
    agent._send = lambda message: None

    def no_signal(self: threading.Event, timeout: float | None = None) -> bool:
        return False

    monkeypatch.setattr(threading.Event, "wait", no_signal)

    with pytest.raises(TimeoutError, match="speak_ended"):
        agent.stream_pcm([b"\x00\x00" * 10], wait=True)


def test_read_loop_ignores_speak_end_from_another_task() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages = iter(
                (
                    json.dumps(
                        {
                            "type": "agent.speak_ended",
                            "event_id": "server-event-other",
                            "task": {"id": "task-other"},
                        }
                    ),
                )
            )

        def recv(self):
            try:
                return next(self.messages)
            except StopIteration as exc:
                raise websocket.WebSocketConnectionClosedException from exc

    agent = LiteAudioAgent("ws://unused")
    agent._ws = Socket()
    agent._active_speak_task_id = "task-current"

    agent._read_loop()

    assert not agent._speak_ended.is_set()


def test_read_loop_correlates_playback_events_by_server_task_id() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages = iter(
                (
                    json.dumps(
                        {
                            "type": "agent.speak_started",
                            "event_id": "server-event-started",
                            "task": {"id": "task-current"},
                        }
                    ),
                    json.dumps(
                        {
                            "type": "agent.speak_ended",
                            "event_id": "server-event-ended",
                            "task": {"id": "task-current"},
                        }
                    ),
                )
            )

        def recv(self):
            try:
                return next(self.messages)
            except StopIteration as exc:
                raise websocket.WebSocketConnectionClosedException from exc

    agent = LiteAudioAgent("ws://unused")
    agent._ws = Socket()
    agent._active_speak_event_id = "client-event-id"

    agent._read_loop()

    assert agent._speak_ended.is_set()


def test_read_loop_wakes_playback_waiter_when_session_closes() -> None:
    class Socket:
        def __init__(self) -> None:
            self.messages = iter(
                (
                    json.dumps(
                        {
                            "type": "session.state_updated",
                            "state": "closed",
                        }
                    ),
                )
            )

        def recv(self):
            try:
                return next(self.messages)
            except StopIteration as exc:
                raise websocket.WebSocketConnectionClosedException from exc

    agent = LiteAudioAgent("ws://unused")
    agent._ws = Socket()

    agent._read_loop()

    with pytest.raises(ConnectionError, match="closed"):
        agent.stream_pcm([b"\x00\x00" * 10], wait=True)


def test_read_loop_survives_idle_socket_timeout() -> None:
    class Socket:
        def __init__(self) -> None:
            self.calls = 0

        def recv(self):
            self.calls += 1
            if self.calls == 1:
                raise websocket.WebSocketTimeoutException("idle")
            if self.calls == 2:
                return json.dumps(
                    {
                        "type": "agent.speak_ended",
                        "event_id": "server-event-ended",
                        "task": {"id": "task-current"},
                    }
                )
            raise websocket.WebSocketConnectionClosedException

    agent = LiteAudioAgent("ws://unused")
    agent._ws = Socket()
    agent._active_speak_task_id = "task-current"

    agent._read_loop()

    assert agent._speak_ended.is_set()
