"""Contract: ProviderError hierarchy maps to stable HTTP statuses (Change T 3.7).

The API boundary maps every provider domain error through the shared
error envelope using the error's own http_status hint — never a blanket 500.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tts import create_app
from tts.engines.base import TTSEngine, TTSRequest
from tts.providers import errors

EXPECTED_STATUS = [
    (errors.CapabilityError, 400),
    (errors.ProfileNotFoundError, 404),
    (errors.ProfileUnauthorizedError, 403),
    (errors.OverloadError, 429),
    (errors.DeadlineExceededError, 408),
    (errors.CancelledError, 499),
    (errors.ProviderUnavailableError, 503),
    (errors.ProviderInferenceError, 502),
]


class _FailingEngine(TTSEngine):
    """Engine that raises the configured provider error on every synthesis."""

    name = "failing"

    def __init__(self, exc: errors.ProviderError) -> None:
        self._exc = exc

    @classmethod
    def from_config(cls, cfg: dict) -> "_FailingEngine":  # pragma: no cover
        raise NotImplementedError

    def synthesize(self, req: TTSRequest) -> None:  # pragma: no cover
        raise self._exc


@pytest.mark.parametrize(("error_type", "expected_status"), EXPECTED_STATUS)
def test_provider_error_maps_to_status(
    error_type: type[errors.ProviderError], expected_status: int
) -> None:
    app = create_app()
    with TestClient(app) as client:
        app.state.engine = _FailingEngine(error_type("boom"))
        app.state.engine_ready = True
        resp = client.post("/v1/speech", json={"text": "xin chào"})
    assert resp.status_code == expected_status
    assert resp.json()["error"]["message"]
