"""P0-FB-010c r1: behaviour against the REAL livekit-plugins-lemonslice (skipped without the extra).

No LemonSlice call: a loopback HTTP server stands in for the provider.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytest.importorskip("livekit.plugins.lemonslice")

from backend.application.clients.avatar.lemonslice import (  # noqa: E402
    LemonSliceError,
    LemonSliceRenderBackend,
)
from backend.application.render.engines_base import StartOptions  # noqa: E402

from .lemonslice_double import FakeRoom  # noqa: E402
from .test_lemonslice_backend import LS_KEY, settings  # noqa: E402
from .test_lemonslice_races import until  # noqa: E402

pytestmark = pytest.mark.timeout(60)

SRC = str(Path(__file__).resolve().parents[2] / "src")


def test_plugin_loads_on_the_calling_thread_before_the_loop_thread_needs_it():
    """The plugin registers itself at import and requires the MAIN thread."""
    code = (
        "import threading, sys\n"
        "from backend.application.clients.avatar.lemonslice import "
        "LemonSliceRenderBackend, LemonSliceSettings\n"
        "s = LemonSliceSettings(livekit_url='wss://x', livekit_api_key='k',"
        " livekit_api_secret='s'*32, lemonslice_api_key='ls')\n"
        "b = LemonSliceRenderBackend(s)\n"  # main thread: loads the plugin here
        "out = []\n"
        "t = threading.Thread(target=lambda: out.append(b._session_client()))\n"
        "t.start(); t.join()\n"
        "assert out, 'client factory failed on a worker thread'\n"
    )
    env = dict(os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", ""))
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[-800:]


class _Provider:
    """Loopback provider. mode 'slow': headers + first bytes at once, rest after ``delay``."""

    def __init__(self, mode: str, delay: float = 0.0) -> None:
        outer = self
        self.mode, self.delay = mode, delay
        self.creates: list[str] = []
        self.terminates: list[str] = []

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                if body.get("event") == "terminate":
                    outer.terminates.append(self.path)
                    data = b"{}"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                outer.creates.append(self.path)
                if outer.mode == "html":
                    data = b"<html>nope</html>"
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                sid = (
                    "https://provider.invalid/private-response-body"
                    if outer.mode == "bad_id"
                    else "ls-slow-1"
                )
                data = json.dumps({"session_id": sid}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data[:5])
                    self.wfile.flush()
                    time.sleep(outer.delay)
                    self.wfile.write(data[5:])
                except OSError:
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def provider(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    made: list[_Provider] = []

    def make(mode, delay=0.0):
        p = _Provider(mode, delay)
        made.append(p)
        return p

    yield make
    for p in made:
        p.close()


def _backend(p: _Provider, **kw):
    room = FakeRoom()
    backend = LemonSliceRenderBackend(
        settings(api_base=p.url, keepalive_s=0, **kw), room_factory=lambda: room
    )
    return backend, room


def test_response_slower_than_the_deadline_is_still_terminated(provider):
    p = provider("slow", delay=1.0)
    backend, room = _backend(p, request_timeout_s=0.3)
    with pytest.raises(LemonSliceError):
        backend.start(StartOptions())
    assert until(lambda: len(p.terminates) == 1, 10)  # the late session id was recovered
    assert len(p.creates) == 1 and p.terminates[0].endswith("/sessions/ls-slow-1/control")
    backend.stop_all()
    assert len(p.creates) == 1 and len(p.terminates) == 1


def test_plugin_failure_logs_contain_no_provider_url_body_or_traceback(provider, caplog):
    p = provider("html")
    backend, room = _backend(p)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LemonSliceError):
            backend.start(StartOptions())
    backend.stop_all()
    assert p.creates
    for rec in caplog.records:
        text = rec.getMessage() + " " + json.dumps(rec.__dict__, default=str)
        assert p.url not in text and "127.0.0.1" not in text and LS_KEY not in text
        assert not rec.exc_info and not rec.exc_text


def test_provider_text_in_a_malformed_session_id_never_reaches_any_log_record(provider, caplog):
    p = provider("bad_id")
    backend, room = _backend(p)
    with caplog.at_level(logging.DEBUG, logger="livekit.plugins.lemonslice"):
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(LemonSliceError):
                backend.start(StartOptions())
    backend.stop_all()
    assert p.creates and not p.terminates  # an unusable id is never interpolated into a URL
    plugin_records = [r for r in caplog.records if r.name.startswith("livekit.plugins.lemonslice")]
    assert plugin_records  # the plugin did log; every record is the static replacement
    for rec in plugin_records:
        assert rec.getMessage().startswith("lemonslice plugin event level=")
        assert not rec.args and not rec.exc_info
    for fragment in ("provider.invalid", "private-response-body", "https://"):
        assert fragment not in caplog.text
