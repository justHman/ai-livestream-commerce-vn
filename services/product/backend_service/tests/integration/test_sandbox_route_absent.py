"""Three-layer sandbox verification contracts."""

from __future__ import annotations


from fastapi.testclient import TestClient

from backend.config import AppConfig
from backend.application.render.engines_base import FullPipelineBackend, StartOptions, StartResult
from conftest import make_deps as _Deps  # noqa: F401


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
    from backend.main import create_app

    config = AppConfig(render_backend="cloud_liveavatar", app_env="dev")
    dependencies = _Deps(
        backend=backend,
        config=config,
    )
    return TestClient(create_app(config=config, deps=dependencies))


def test_sandbox_verify_route_absent_from_production_app() -> None:
    """Sandbox verification is not a production backend route (1.25)."""
    with _client(_VerificationBackend()) as client:
        response = client.post(
            "/api/v1/admin/sandbox/verify",
            json={"avatar_id": "avatar-1", "speech_text": "Xin chào"},
        )
    assert response.status_code == 404


def test_sandbox_verify_route_absent_even_with_valid_token() -> None:
    with _client(_VerificationBackend()) as client:
        response = client.post(
            "/api/v1/admin/sandbox/verify",
            json={},
            headers={"authorization": "Bearer whatever"},
        )
    assert response.status_code == 404


def test_verification_cleanup_is_idempotent() -> None:
    backend = _VerificationBackend()
    backend.stop("verify-session")
    backend.stop("verify-session")

    assert backend.stopped == ["verify-session"]
