"""Three-layer sandbox verification contracts."""

from __future__ import annotations

import threading
import time

from fastapi.testclient import TestClient

from core.api import v1
from core.config import AppConfig
from core.engine_manager import EngineManager
from core.render.base import FullPipelineBackend, StartOptions, StartResult
from core.store import InMemorySessionStore


class _VerificationBackend(FullPipelineBackend):
    name = "cloud"

    def __init__(self, fail_at: str | None = None) -> None:
        self.fail_at = fail_at
        self.stopped: list[str] = []
        self.calls: list[str] = []

    def verify_credentials(self) -> dict:
        self.calls.append("credentials")
        if self.fail_at == "credentials":
            raise PermissionError("secret-key-value")
        return {"credits_available": True}

    def start(self, opts: StartOptions) -> StartResult:
        self.calls.append("connectivity")
        if self.fail_at == "connectivity":
            raise ConnectionError("internal-provider-payload")
        return StartResult("verify-session", "wss://livekit.example", "client-token")

    def say(self, session_id: str, text: str, generate: bool = True) -> str:
        self.calls.append("speech")
        if self.fail_at == "speech":
            raise TimeoutError("raw-provider-timeout")
        return "Xin chào từ phiên kiểm tra."

    def interrupt(self, session_id: str) -> None:
        return None

    def stop(self, session_id: str) -> None:
        if session_id not in self.stopped:
            self.stopped.append(session_id)


def _client(backend: FullPipelineBackend) -> TestClient:
    from core.server import create_app

    config = AppConfig(render_backend="cloud_liveavatar", app_env="dev")
    dependencies = v1.V1Deps(
        backend=backend,
        store=InMemorySessionStore(),
        hub=v1.ControlHub(),
        engine_manager=EngineManager(),
        config=config,
    )
    return TestClient(create_app(config=config, deps=dependencies))


def test_sandbox_verification_passes_all_layers_and_cleans_up() -> None:
    backend = _VerificationBackend()
    with _client(backend) as client:
        response = client.post(
            "/api/v1/admin/sandbox/verify",
            json={"avatar_id": "avatar-1", "speech_text": "Xin chào"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["ready"] is True
    assert [layer["status"] for layer in body["layers"]] == ["pass", "pass", "pass"]
    assert all(layer["latency_ms"] >= 0 for layer in body["layers"])
    assert backend.calls == ["credentials", "connectivity", "speech"]
    assert backend.stopped == ["verify-session"]
    assert "client-token" not in str(body)


def test_connectivity_failure_skips_speech_and_preserves_prior_result() -> None:
    backend = _VerificationBackend(fail_at="connectivity")
    with _client(backend) as client:
        response = client.post("/api/v1/admin/sandbox/verify", json={})

    body = response.json()
    assert body["ready"] is False
    assert [layer["status"] for layer in body["layers"]] == ["pass", "fail", "skipped"]
    assert backend.calls == ["credentials", "connectivity"]
    assert "internal-provider-payload" not in str(body)


def test_speech_failure_still_cleans_up_and_sanitizes_error() -> None:
    backend = _VerificationBackend(fail_at="speech")
    with _client(backend) as client:
        response = client.post("/api/v1/admin/sandbox/verify", json={})

    body = response.json()
    assert body["ready"] is False
    assert body["layers"][2]["error"] == "speech verification failed"
    assert "raw-provider-timeout" not in str(body)
    assert backend.stopped == ["verify-session"]


class _LateStartBackend(_VerificationBackend):
    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()

    def start(self, opts: StartOptions) -> StartResult:
        self.calls.append("connectivity")
        self.release.wait(timeout=2)
        return StartResult("late-session", "wss://livekit.example", "client-token")


def test_connectivity_timeout_cleans_session_that_finishes_late(monkeypatch) -> None:
    backend = _LateStartBackend()
    monkeypatch.setattr(v1, "SANDBOX_LAYER_TIMEOUT_SEC", 0.05)
    with _client(backend) as client:
        timer = threading.Timer(0.1, backend.release.set)
        timer.start()
        response = client.post("/api/v1/admin/sandbox/verify", json={})
        timer.join()
        deadline = time.monotonic() + 1
        while "late-session" not in backend.stopped and time.monotonic() < deadline:
            time.sleep(0.01)

    assert response.json()["ready"] is False
    assert "late-session" in backend.stopped


def test_verification_cleanup_is_idempotent() -> None:
    backend = _VerificationBackend()
    backend.stop("verify-session")
    backend.stop("verify-session")

    assert backend.stopped == ["verify-session"]
