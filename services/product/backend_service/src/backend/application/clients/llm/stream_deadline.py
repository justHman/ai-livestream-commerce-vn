"""Per-request streaming cancellation and deadlines, including blocking reads."""

from contextlib import contextmanager
from contextvars import ContextVar
import socket
import threading
import time


TOTAL_TIMEOUT_S = 60.0
FIRST_CONTENT_TIMEOUT_S = 15.0
_CANCEL: ContextVar[threading.Event | None] = ContextVar("llm_cancel", default=None)


@contextmanager
def streaming_cancellation(event):
    token = _CANCEL.set(event)
    try:
        yield
    finally:
        _CANCEL.reset(token)


class StreamDeadline:
    def __init__(self, total, first):
        now = time.monotonic()
        self.total, self.first = now + total, now + first
        self.content = threading.Event()
        self.done = threading.Event()
        self.expired = threading.Event()
        self.cancel = _CANCEL.get()
        self.sock = None
        self.response = None
        self.worker = threading.Thread(target=self._watch, daemon=True)

    def check(self):
        now = time.monotonic()
        if (
            self.expired.is_set()
            or (self.cancel is not None and self.cancel.is_set())
            or now >= self.total
            or (not self.content.is_set() and now >= self.first)
        ):
            raise TimeoutError("LLM streaming deadline/cancellation exceeded")

    def _interrupt(self):
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self.response is not None:
            self.response.close()

    def _watch(self):
        while not self.done.wait(0.02):
            try:
                self.check()
            except TimeoutError:
                self.expired.set()
                try:
                    self._interrupt()
                except Exception:
                    pass
                return

    def capture(self, stream):
        if stream is not None:
            self.sock = stream.get_extra_info("socket")
            if self.expired.is_set():
                self._interrupt()

    def trace(self, event, info):
        if event in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            self.capture(info["return_value"])

    def __enter__(self):
        self.worker.start()
        return self

    def __exit__(self, *args):
        self.done.set()
        self.worker.join(timeout=0.1)
