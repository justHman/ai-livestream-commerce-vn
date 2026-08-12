"""Provider domain error types and HTTP mapping hints (Change T task 2.7).

Actual status mapping happens at the API boundary; here we only verify the
error hierarchy, message passthrough, and stable mapping hints.
"""

from __future__ import annotations

import pytest

from tts.providers import errors


@pytest.mark.parametrize(
    ("error_type", "expected_status"),
    [
        (errors.CapabilityError, 400),
        (errors.ProfileNotFoundError, 404),
        (errors.ProfileUnauthorizedError, 403),
        (errors.OverloadError, 429),
        (errors.DeadlineExceededError, 408),
        (errors.CancelledError, 499),
        (errors.ProviderUnavailableError, 503),
        (errors.ProviderInferenceError, 502),
    ],
)
def test_error_mapping_hints(error_type: type[errors.ProviderError], expected_status: int) -> None:
    exc = error_type("boom")
    assert exc.http_status == expected_status
    assert exc.message == "boom"
    assert isinstance(exc, errors.ProviderError)


def test_provider_error_is_runtime_error() -> None:
    assert issubclass(errors.ProviderError, RuntimeError)


def test_base_provider_error_default_status() -> None:
    assert errors.ProviderError("boom").http_status == 500
