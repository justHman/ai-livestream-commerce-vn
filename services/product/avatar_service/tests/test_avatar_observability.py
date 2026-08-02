from __future__ import annotations

import asyncio
import io
import logging
import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from avatar.observability import (
    bind,
    extract_from_headers,
    get_all,
    outbound_headers,
    reset,
    scoped,
    scoped_from_headers,
    setup_logging,
    validate_config,
)
from avatar.observability.logging.config import REDACTION_FIELD, REDACTION_MARKER
from avatar.observability.logging.filters import ContextFilter, StructuredFieldsFilter
from avatar.observability.logging.formatter import ContextFormatter
from avatar.observability.logging.setup import reset_logging


@contextmanager
def removable_temp_dir() -> Generator[Path, None, None]:
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path)


def make_record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "event", (), None)
    record.__dict__.update(extra)
    return record


def render(extra: dict[str, object]) -> str:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(ContextFormatter(service="avatar"))
    handler.addFilter(ContextFilter())
    handler.addFilter(StructuredFieldsFilter())
    logger = logging.getLogger("avatar.observability.test.render")
    old_handlers, old_propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    try:
        logger.info("event", extra=extra)
        return stream.getvalue()
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
        handler.close()
        stream.close()


def render_with_downstream_handler(extra: dict[str, object]) -> tuple[str, logging.LogRecord]:
    captured: list[logging.LogRecord] = []

    class CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    stream = io.StringIO()
    safe_handler = logging.StreamHandler(stream)
    safe_handler.setFormatter(ContextFormatter(service="avatar"))
    safe_handler.addFilter(StructuredFieldsFilter())
    downstream_handler = CaptureHandler()
    logger = logging.getLogger("avatar.observability.test.downstream")
    old_handlers, old_propagate = logger.handlers[:], logger.propagate
    logger.handlers, logger.propagate = [safe_handler, downstream_handler], False
    logger.setLevel(logging.INFO)
    try:
        logger.info("event", extra=extra)
        return stream.getvalue(), captured[0]
    finally:
        logger.handlers, logger.propagate = old_handlers, old_propagate
        safe_handler.close()
        downstream_handler.close()
        stream.close()


@pytest.fixture(autouse=True)
def clean_observability() -> Generator[None, None, None]:
    reset()
    reset_logging()
    yield
    reset()
    reset_logging()


def test_context_identifiers_are_exact() -> None:
    with scoped(session_id="s", request_id="r", trace_id="t", component="avatar"):
        assert get_all() == {
            "session_id": "s",
            "request_id": "r",
            "trace_id": "t",
            "component": "avatar",
        }


def test_unknown_context_field_is_rejected_without_partial_binding() -> None:
    with pytest.raises(ValueError, match="Unknown context field"):
        bind(request_id="valid", user_id="customer")
    assert get_all() == {}


@pytest.mark.parametrize(
    "value",
    ["", " leading", "has space", "line\nbreak", "ü", "x" * 129, 123],
)
def test_invalid_identifier_is_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="Invalid request_id"):
        bind(request_id=value)
    assert get_all() == {}


def test_nested_scope_restores_previous_context() -> None:
    with scoped(request_id="outer", session_id="session"):
        with scoped(request_id="inner", component="llm"):
            assert get_all()["request_id"] == "inner"
        assert get_all() == {"request_id": "outer", "session_id": "session"}
    assert get_all() == {}


@pytest.mark.asyncio
async def test_concurrent_tasks_keep_isolated_context() -> None:
    async def worker(request_id: str) -> dict[str, str]:
        with scoped(request_id=request_id):
            await asyncio.sleep(0)
            return get_all()

    first, second = await asyncio.gather(worker("first"), worker("second"))
    assert (first, second, get_all()) == (
        {"request_id": "first"},
        {"request_id": "second"},
        {},
    )


def test_context_cleanup_after_success() -> None:
    with scoped(request_id="request"):
        pass
    assert get_all() == {}


def test_context_cleanup_after_error() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with scoped(request_id="request"):
            raise RuntimeError("boom")
    assert get_all() == {}


def test_context_cleanup_after_cancellation() -> None:
    with pytest.raises(asyncio.CancelledError):
        with scoped(request_id="request"):
            raise asyncio.CancelledError
    assert get_all() == {}


def test_inbound_metadata_is_case_insensitive_and_validated() -> None:
    assert extract_from_headers(
        {
            "X-Session-ID": "session-1",
            "x-request-id": "request-1",
            "X-TRACE-ID": "trace/1",
            "x-component": "avatar.api",
            "authorization": "not-context",
        }
    ) == {
        "session_id": "session-1",
        "request_id": "request-1",
        "trace_id": "trace/1",
        "component": "avatar.api",
    }


def test_invalid_inbound_metadata_is_not_bound() -> None:
    with pytest.raises(ValueError, match="Invalid request_id"):
        with scoped_from_headers({"x-request-id": "bad value"}):
            pass
    assert get_all() == {}


def test_scoped_inbound_metadata_restores_on_error() -> None:
    with pytest.raises(RuntimeError, match="boom"):
        with scoped_from_headers({"x-request-id": "request-1"}):
            raise RuntimeError("boom")
    assert get_all() == {}


def test_outbound_metadata_contains_only_validated_context() -> None:
    with scoped(
        session_id="session-1",
        request_id="request-1",
        trace_id="trace-1",
        component="avatar.client",
    ):
        assert outbound_headers() == {
            "x-session-id": "session-1",
            "x-request-id": "request-1",
            "x-trace-id": "trace-1",
            "x-component": "avatar.client",
        }


@pytest.mark.parametrize("level", ["CRITICAL", "TRACE", "", "info", "info "])
def test_unsupported_level_fails_startup(level: str) -> None:
    with pytest.raises(ValueError, match="Invalid LOG_LEVEL"):
        validate_config(level=level)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"service": "other"}, "Invalid SERVICE_NAME"),
        ({"runtime_root": ""}, "LOG_ROOT"),
        ({"retention_days": 0}, "LOG_RETENTION_DAYS"),
        ({"retention_days": True}, "LOG_RETENTION_DAYS"),
        ({"retention_days": 1.0}, "LOG_RETENTION_DAYS"),
        ({"retention_days": "30"}, "LOG_RETENTION_DAYS"),
        ({"color": "always"}, "LOG_COLOR"),
    ],
)
def test_logging_configuration_is_validated(overrides: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_config(**overrides)


def test_retention_accepts_integer_override() -> None:
    assert validate_config(retention_days=7).retention_days == 7


def test_retention_parses_digit_environment_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_RETENTION_DAYS", "7")
    assert validate_config().retention_days == 7


@pytest.mark.parametrize("value", ["7.0", "-1", "+7", " 7", "seven"])
def test_retention_rejects_non_digit_environment_value(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("LOG_RETENTION_DAYS", value)
    with pytest.raises(ValueError, match="LOG_RETENTION_DAYS"):
        validate_config()


def test_setup_is_idempotent_and_closes_owned_handler() -> None:
    with removable_temp_dir() as runtime_root:
        logger = setup_logging(validate_config(runtime_root=runtime_root))
        handler = logger.handlers[-1]
        setup_logging(validate_config(runtime_root=runtime_root))
        assert (
            sum(getattr(item, "_avatar_observability_handler", False) for item in logger.handlers)
            == 1
        )
        reset_logging()
        assert handler._closed
    assert not runtime_root.exists()


def test_approved_context_overrides_untrusted_record_context() -> None:
    with scoped(request_id="bound-request"):
        output = render({"request_id": "forged-request", "event": "started"})
    assert "request_id=bound-request" in output and "forged-request" not in output


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "_token",
        "api_key",
        "_api_key",
        "apiKey",
        "API-KEY",
        "provider_session_token",
        "meta.auth.token",
        "password_hash",
        "cookie",
        "credentials",
    ],
)
def test_sensitive_field_is_replaced_by_stable_redaction_marker(key: str) -> None:
    record = make_record(**{key: "sensitive-value"})
    StructuredFieldsFilter().filter(record)
    assert record.__dict__[REDACTION_FIELD] == REDACTION_MARKER
    assert key not in record.__dict__


def test_downstream_handler_cannot_see_raw_secret() -> None:
    output, record = render_with_downstream_handler(
        {"_api_key": "secret-value", "event": "started"}
    )
    assert REDACTION_MARKER in output and "secret-value" not in output
    assert "_api_key" not in record.__dict__ and "secret-value" not in record.__dict__.values()


@pytest.mark.parametrize(
    "key",
    [
        "prompt",
        "viewer_message",
        "shop_profile",
        "provider_body",
        "request_body",
        "customer_payload",
        "user_id",
        "shop_id",
    ],
)
def test_freeform_and_unapproved_fields_are_omitted(key: str) -> None:
    record = make_record(**{key: "customer-value"})
    StructuredFieldsFilter().filter(record)
    assert key not in record.__dict__


def test_secret_and_freeform_values_do_not_reach_output() -> None:
    output = render(
        {
            "event": "started",
            "api_key": "secret-value",
            "prompt": "customer prompt",
            "viewer_message": "customer message",
        }
    )
    assert REDACTION_MARKER in output
    assert all(
        value not in output for value in ("secret-value", "customer prompt", "customer message")
    )
