"""Tests for the backend observability module.

Concurrent isolation, transport propagation, idempotent setup,
invalid-level startup failure, secret-free output, and structure parity.
"""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from llm.observability import (
    bind,
    extract_from_headers,
    get,
    outbound_headers,
    reset,
    scoped,
    setup_logging,
    validate_config,
)
from llm.observability.logging.config import LoggingConfig as _LoggingConfig
from llm.observability.logging.setup import reset_logging

# ---------------------------------------------------------------------------
# Context tests
# ---------------------------------------------------------------------------


class TestContext:
    def test_bind_and_get(self) -> None:
        reset()
        bind(request_id="r1", session_id="s1")
        assert get("request_id") == "r1"
        assert get("session_id") == "s1"
        assert get("nonexistent") is None

    def test_bind_invalid_field(self) -> None:
        reset()
        with pytest.raises(ValueError, match="Unknown context field"):
            bind(invalid_field="x")

    def test_reset_specific_keys(self) -> None:
        reset()
        bind(request_id="r1", session_id="s1")
        reset("request_id")
        assert get("request_id") is None
        assert get("session_id") == "s1"

    def test_reset_all(self) -> None:
        reset()
        bind(request_id="r1")
        reset()
        assert get("request_id") is None

    def test_scoped_context_manager(self) -> None:
        reset()
        bind(request_id="outer")
        with scoped(request_id="inner"):
            assert get("request_id") == "inner"
        assert get("request_id") == "outer"

    def test_scoped_restore_on_error(self) -> None:
        reset()
        bind(request_id="outer")
        try:
            with scoped(request_id="inner"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert get("request_id") == "outer"

    def test_concurrent_isolation(self) -> None:
        """ContextVars must be isolated across threads."""
        reset()
        import threading

        results: list[str] = []

        def worker(val: str) -> None:
            bind(request_id=val)
            results.append(get("request_id"))

        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert sorted(results) == ["t1", "t2"]

    def test_nested_scoped_restore(self) -> None:
        reset()
        bind(request_id="a", session_id="s1")
        with scoped(request_id="b"):
            assert get("request_id") == "b"
            assert get("session_id") == "s1"
            with scoped(request_id="c"):
                assert get("request_id") == "c"
                assert get("session_id") == "s1"
            assert get("request_id") == "b"
        assert get("request_id") == "a"
        assert get("session_id") == "s1"


# ---------------------------------------------------------------------------
# Transport metadata tests
# ---------------------------------------------------------------------------


class TestTransport:
    def test_extract_from_headers(self) -> None:
        headers = {
            "x-request-id": "req-1",
            "x-trace-id": "trace-1",
            "x-session-id": "sess-1",
        }
        result = extract_from_headers(headers)
        assert result["request_id"] == "req-1"
        assert result["trace_id"] == "trace-1"
        assert result["session_id"] == "sess-1"
        assert "x-span-id" not in result

    def test_outbound_headers(self) -> None:
        reset()
        bind(request_id="req-1", trace_id="trace-1")
        headers = outbound_headers()
        assert headers["x-request-id"] == "req-1"
        assert headers["x-trace-id"] == "trace-1"

    def test_outbound_headers_empty(self) -> None:
        reset()
        assert outbound_headers() == {}


# ---------------------------------------------------------------------------
# Config validation tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_valid_level(self) -> None:
        cfg = validate_config(level="DEBUG")
        assert cfg.level == "DEBUG"

    def test_invalid_level(self) -> None:
        with pytest.raises(ValueError, match="Invalid LOG_LEVEL"):
            validate_config(level="TRACE")

    def test_invalid_service(self) -> None:
        with pytest.raises(ValueError, match="Invalid SERVICE_NAME"):
            validate_config(service="")

    def test_invalid_environment(self) -> None:
        with pytest.raises(ValueError, match="Invalid APP_ENV"):
            validate_config(environment="prod")

    def test_invalid_log_dir_relative(self) -> None:
        with pytest.raises(ValueError, match="LOG_DIR must be absolute"):
            validate_config(log_dir="relative/path")

    def test_invalid_retention(self) -> None:
        with pytest.raises(ValueError, match="LOG_RETENTION_DAYS must be >= 1"):
            validate_config(retention_days=0)

    def test_valid_log_dir_absolute(self) -> None:
        validate_config(log_dir=str(Path("/tmp/logs").resolve()))

    def test_approved_field(self) -> None:
        assert _LoggingConfig.is_approved_field("request_id")
        assert _LoggingConfig.is_approved_field("session_id")
        assert not _LoggingConfig.is_approved_field("credit_card")

    def test_sanitize_extra(self) -> None:
        extra = {"request_id": "r1", "password": "secret", "api_key": "abc"}
        sanitized = _LoggingConfig.sanitize_extra(extra)
        assert "request_id" in sanitized
        assert "password" not in sanitized
        assert "api_key" not in sanitized


# ---------------------------------------------------------------------------
# Idempotent setup tests
# ---------------------------------------------------------------------------


class TestSetup:
    def setup_method(self) -> None:
        reset_logging()

    def teardown_method(self) -> None:
        """Close all handlers (reset_logging closes streams) and reset flag."""
        reset_logging()

    def test_setup_logging_once(self) -> None:
        setup_logging(level="DEBUG")
        root = logging.getLogger()
        assert root.level == logging.DEBUG
        assert len(root.handlers) > 0

    def test_setup_logging_idempotent(self) -> None:
        setup_logging(level="DEBUG")
        handler_count = len(logging.getLogger().handlers)
        setup_logging(level="INFO")
        assert len(logging.getLogger().handlers) == handler_count

    def test_setup_logging_with_log_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            setup_logging(level="DEBUG", log_dir=tmp)
            logger = logging.getLogger()
            # Should have console + daily + active = 3 handlers
            assert len(logger.handlers) >= 1
            reset_logging()
            # reset_logging must close streams so tmpdir is removable
        # If streams leaked, Windows raises PermissionError on cleanup here.

    def test_log_output_contains_approved_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            setup_logging(level="DEBUG", log_dir=tmp)
            logger = logging.getLogger("test_logger")
            bind(request_id="r1", session_id="s1")
            logger.info("test message")
            reset_logging()
            # Active-session file written under config.log_dir/<service>.log
            log_file = Path(tmp) / "llm.log"
            assert log_file.exists()
            content = log_file.read_text()
            assert "test message" in content
            # Context fields are injected (approved-field allowlist).
            assert "request_id=r1" in content
            assert "session_id=s1" in content

    def test_log_output_no_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            setup_logging(level="DEBUG", log_dir=tmp, service="test")
            logger = logging.getLogger("test_secret")
            logger.info("user data", extra={"password": "hunter2", "api_key": "abc123"})
            reset_logging()
            log_file = Path(tmp) / "test.log"
            assert log_file.exists()
            content = log_file.read_text()
            assert "user data" in content
            assert "hunter2" not in content
            assert "abc123" not in content

    def test_setup_failure_closes_created_handlers(self) -> None:
        """If a later handler fails, already-opened streams must be closed."""
        with (
            TemporaryDirectory() as tmp,
            patch("llm.observability.logging.setup.ActiveSessionHandler") as mock_active,
        ):
            mock_active.side_effect = OSError("cannot create active handler")
            with pytest.raises(OSError):
                setup_logging(level="DEBUG", log_dir=tmp)
            # reset_logging will itself close the leaked console/daily handlers.
            if logging.getLogger().handlers:
                reset_logging()


# ---------------------------------------------------------------------------
# Structure parity: all four services must have the same observability files
# ---------------------------------------------------------------------------


class TestStructureParity:
    """Verify that all four product services have the same observability layout.

    This test runs in the backend service context but checks sibling services.
    Uses the worktree-relative path to find the services root.
    """

    OBS_FILES = frozenset(
        {
            "observability/__init__.py",
            "observability/context.py",
            "observability/logging/__init__.py",
            "observability/logging/config.py",
            "observability/logging/filters.py",
            "observability/logging/formatter.py",
            "observability/logging/daily_handler.py",
            "observability/logging/active_session_handler.py",
            "observability/logging/setup.py",
        }
    )

    def _service_root(self) -> Path:
        # Walk up from this file until we reach the directory containing
        # services/product/ — robust to the pytest cwd.
        p = Path(__file__).resolve().parent
        while p != p.parent:
            if (p / "services" / "product").is_dir():
                return p / "services" / "product"
            p = p.parent
        raise RuntimeError("could not locate services/product anchor")

    def test_all_services_have_observability(self) -> None:
        root = self._service_root()
        expected = set(self.OBS_FILES)
        for svc in ("llm_service", "llm_service", "tts_service", "avatar_service"):
            pkg = svc.replace("_service", "")
            src_dir = root / svc / "src" / pkg
            actual = set()
            for fpath in src_dir.rglob("*.py"):
                rel = fpath.relative_to(src_dir).as_posix()
                if rel in expected:
                    actual.add(rel)
            missing = expected - actual
            assert not missing, f"{svc} missing: {missing}"

    def test_no_cross_imports(self) -> None:
        """Verify no service imports from another service's observability."""
        root = self._service_root()
        for svc in ("llm_service", "tts_service", "avatar_service"):
            pkg = svc.replace("_service", "")
            src_dir = root / svc / "src" / pkg
            for fpath in src_dir.rglob("*.py"):
                content = fpath.read_text()
                for other_pkg in ("backend", "llm", "tts", "avatar"):
                    if other_pkg == pkg:
                        continue
                    if f"from {other_pkg}." in content or f"import {other_pkg}." in content:
                        pytest.fail(f"{fpath.relative_to(root / svc)} imports from {other_pkg}")
