"""LITE-mode agent — streams PCM audio to LiveAvatar over the session WebSocket.

In LITE mode YOU run the brain (STT -> LLM -> TTS); LiveAvatar only renders
video. Video reaches the *frontend* directly over LiveKit/WebRTC — it NEVER
passes through this backend. This module's only job is the audio side:
connect to `ws_url`, wait for `connected`, then push PCM (16-bit/24 kHz/mono)
as `agent.speak` chunks and close each utterance with `agent.speak_end`.

Protocol (see lite-mode-guide.md):
  send:    agent.speak / agent.speak_end / agent.interrupt /
           agent.start_listening / agent.stop_listening / session.keep_alive
  receive: session.state_updated / agent.speak_started / agent.speak_ended

This is sync-WebSocket (the `websocket-client` package) so it composes with
sync TTS generators. Use one LiteAudioAgent per session.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Iterable, Optional
from uuid import uuid4

import websocket  # websocket-client

from ..sdk import audio


class LiteAudioAgent:
    """Drives the LiveAvatar LITE audio WebSocket for one session.

    Parameters
    ----------
    ws_url : str
        The `ws_url` returned by POST /v1/sessions/start in LITE mode.
    on_event : callable or None
        Optional callback(dict) for every server event received.
    """

    def __init__(
        self,
        ws_url: str,
        on_event: Optional[Callable[[dict], None]] = None,
    ) -> None:
        self.ws_url = ws_url
        self.on_event = on_event
        self._ws: Optional[websocket.WebSocket] = None
        self._connected = threading.Event()
        self._speak_ended = threading.Event()
        self._active_speak_event_id: Optional[str] = None
        self._active_speak_task_id: Optional[str] = None
        self._connection_error: Optional[str] = None
        self._streaming = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._keepalive: Optional[threading.Thread] = None
        self._closed = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self, timeout: float = 10.0) -> None:
        """Open the WebSocket and block until the server reports connected.

        Events sent before `connected` are silently dropped, so callers
        MUST wait for this to return before speaking.
        """
        self._ws = websocket.create_connection(self.ws_url, timeout=timeout)
        self._ws.settimeout(None)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        if not self._connected.wait(timeout):
            raise TimeoutError("LiveAvatar LITE WS did not reach 'connected'")
        # Start keep-alive once connected (5-min idle timeout).
        self._keepalive = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keepalive.start()

    def _read_loop(self) -> None:
        while not self._closed and self._ws is not None:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            except Exception:
                break
            if not raw:
                continue
            try:
                evt = json.loads(raw)
            except (ValueError, TypeError):
                continue

            etype = evt.get("type")
            task_id = (evt.get("task") or {}).get("id")
            if etype == "session.state_updated":
                state = evt.get("state")
                if state == "connected":
                    self._connected.set()
                elif state in {"closing", "closed"}:
                    self._connection_error = f"LiveAvatar LITE session {state}"
                    self._speak_ended.set()
            elif etype == "agent.speak_started" and task_id:
                self._active_speak_task_id = task_id
            elif (
                etype == "agent.speak_ended"
                and task_id
                and task_id == self._active_speak_task_id
            ):
                self._speak_ended.set()

            if self.on_event:
                self.on_event(evt)

    def _keepalive_loop(self, interval: float = 120.0) -> None:
        while not self._closed:
            time.sleep(interval)
            self._send({"type": "session.keep_alive", "event_id": f"ka-{uuid4()}"})

    # ------------------------------------------------------------------
    # Low-level send
    # ------------------------------------------------------------------

    def _send(self, msg: dict) -> None:
        if self._ws is None or self._closed:
            return
        try:
            self._ws.send(json.dumps(msg))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Listening state (visual cues)
    # ------------------------------------------------------------------

    def start_listening(self) -> None:
        self._send({"type": "agent.start_listening", "event_id": f"sl-{uuid4()}"})

    def stop_listening(self) -> None:
        self._send({"type": "agent.stop_listening", "event_id": f"el-{uuid4()}"})

    # ------------------------------------------------------------------
    # Speaking
    # ------------------------------------------------------------------

    def _begin_speaking(self, event_id: str) -> None:
        if self._connection_error:
            raise ConnectionError(self._connection_error)
        self._active_speak_event_id = event_id
        self._active_speak_task_id = None
        self._speak_ended.clear()

    def _wait_for_playback(self, timeout: float) -> None:
        if not self._speak_ended.wait(timeout=timeout):
            raise TimeoutError(
                f"LiveAvatar agent.speak_ended not received within {timeout:g} seconds"
            )
        if self._connection_error:
            raise ConnectionError(self._connection_error)

    def speak_pcm(self, pcm_24k_mono: bytes, wait: bool = True) -> None:
        """Send one complete utterance (already 24 kHz/16-bit/mono PCM)."""
        event_id = f"speak-{uuid4()}"
        self._begin_speaking(event_id)
        for chunk in audio.chunk_pcm(pcm_24k_mono):
            self._send(
                {"type": "agent.speak", "event_id": event_id, "audio": audio.b64(chunk)}
            )
        self._send({"type": "agent.speak_end", "event_id": event_id})
        if wait:
            self._wait_for_playback(60.0)

    def stream_pcm(
        self,
        pcm_stream: Iterable[bytes],
        source_rate: int = 24_000,
        wait: bool = True,
    ) -> None:
        """Stream TTS PCM chunks as they arrive under one event_id.

        Parameters
        ----------
        pcm_stream : iterable of bytes
            16-bit mono PCM chunks from your TTS at `source_rate`.
        source_rate : int
            Sample rate of the incoming chunks; resampled to 24 kHz.
        wait : bool
            Block until the avatar finishes (agent.speak_ended).
        """
        event_id = f"speak-{uuid4()}"
        self._begin_speaking(event_id)
        self._streaming.set()
        buffer = b""
        target = audio.FIRST_CHUNK

        for pcm in pcm_stream:
            if not self._streaming.is_set():
                break
            if source_rate != audio.TARGET_RATE:
                pcm = audio.resample_to_24k(pcm, source_rate)
            buffer += pcm
            while len(buffer) >= target:
                chunk, buffer = buffer[:target], buffer[target:]
                self._send(
                    {"type": "agent.speak", "event_id": event_id, "audio": audio.b64(chunk)}
                )
                target = audio.NEXT_CHUNK

        if self._streaming.is_set() and buffer:
            self._send(
                {"type": "agent.speak", "event_id": event_id, "audio": audio.b64(buffer)}
            )
        self._send({"type": "agent.speak_end", "event_id": event_id})
        if wait:
            self._wait_for_playback(120.0)

    def interrupt(self) -> None:
        """Stop the current stream and tell the avatar to stop speaking."""
        self._streaming.clear()
        self._send({"type": "agent.interrupt"})

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
            self._ws = None
