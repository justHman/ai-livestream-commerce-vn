from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from backend.observability.logging.active_session_handler import (
    ACTIVE,
    CLOSED,
    INACTIVE,
    ActiveSessionHandler,
)
from backend.observability.logging.platform_collector import (
    PlatformCollector,
    normalize_event_line,
)

SERVICE = "backend"


@contextmanager
def removable_temp_dir() -> Generator[Path, None, None]:
    path = Path(tempfile.mkdtemp())
    try:
        yield path
    finally:
        shutil.rmtree(path)


def make_logger(active_root: Path) -> tuple[logging.Logger, ActiveSessionHandler]:
    handler = ActiveSessionHandler(service=SERVICE, active_root=active_root)
    logger = logging.getLogger(f"test.active_session.{SERVICE}")
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.INFO)
    return logger, handler


def make_record(message: str) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, __file__, 1, message, (), None)


class FailingStream:
    """Stream that fails on write and records whether it was closed."""

    def __init__(self) -> None:
        self.closed = False
        self.close_called = False

    def write(self, _text: str) -> None:
        raise OSError("disk full")

    def flush(self) -> None:
        raise OSError("disk full")

    def close(self) -> None:
        self.close_called = True
        self.closed = True


def product_path(active_root: Path) -> Path:
    return active_root.joinpath("product", f"{SERVICE}.log")


def run_collector(
    module: str, args: list[str], input_text: str
) -> subprocess.CompletedProcess[str]:
    src = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-m", module, *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )


def test_first_start_truncates_empty_path() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        try:
            handler.start_session()
            logger.info("first")
            assert product_path(active_root).read_text(encoding="utf-8") == "first\n"
        finally:
            logger.handlers.clear()
            handler.close()


def test_new_session_overwrites_previous() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        try:
            handler.start_session()
            logger.info("session-a")
            handler.end_session()
            handler.start_session()
            logger.info("session-b")
            assert product_path(active_root).read_text(encoding="utf-8") == "session-b\n"
        finally:
            logger.handlers.clear()
            handler.close()


def test_emits_append_within_session() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        try:
            handler.start_session()
            logger.info("one")
            logger.info("two")
            logger.info("three")
            assert product_path(active_root).read_text(encoding="utf-8") == "one\ntwo\nthree\n"
        finally:
            logger.handlers.clear()
            handler.close()


def test_file_retained_after_end_session() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        path = product_path(active_root)
        try:
            handler.start_session()
            logger.info("kept")
            handler.end_session()
            assert path.exists()
            assert path.read_text(encoding="utf-8") == "kept\n"
        finally:
            logger.handlers.clear()
            handler.close()


def test_constructed_inactive_and_emit_before_start_drops() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        try:
            assert handler.state == INACTIVE
            logger.info("dropped")
            assert not product_path(active_root).exists()
        finally:
            logger.handlers.clear()
            handler.close()


def test_emit_after_end_drops_without_reopening() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        path = product_path(active_root)
        try:
            handler.start_session()
            logger.info("kept")
            handler.end_session()
            logger.info("dropped")
            assert path.read_text(encoding="utf-8") == "kept\n"
        finally:
            logger.handlers.clear()
            handler.close()


def test_start_restarts_after_end() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        path = product_path(active_root)
        try:
            handler.start_session()
            logger.info("first")
            handler.end_session()
            assert handler.state == INACTIVE
            handler.start_session()
            assert handler.state == ACTIVE
            logger.info("second")
            assert path.read_text(encoding="utf-8") == "second\n"
        finally:
            logger.handlers.clear()
            handler.close()


def test_close_is_terminal_and_idempotent() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        try:
            handler.start_session()
            logger.info("kept")
            handler.close()
            handler.close()
            assert handler.state == CLOSED
            with pytest.raises(RuntimeError, match="closed"):
                handler.start_session()
            logger.info("dropped-after-close")
            assert product_path(active_root).read_text(encoding="utf-8") == "kept\n"
        finally:
            logger.handlers.clear()
            handler.close()


def test_no_session_id_in_filename_or_content() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        session_id = "hostile/../../evil"
        try:
            handler.start_session()
            logger.info("session=%s", session_id)
            for path in active_root.rglob("*"):
                assert session_id not in str(path)
            assert product_path(active_root).exists()
        finally:
            logger.handlers.clear()
            handler.close()


def test_platform_group_and_platform_services() -> None:
    with removable_temp_dir() as active_root:
        for service in ("livekit", "lmcache", "postgres", "redis"):
            handler = ActiveSessionHandler(
                service=service, group="platform", active_root=active_root
            )
            handler.start_session()
            handler.emit(make_record("ready"))
            handler.end_session()
            handler.close()
            assert (
                active_root.joinpath("platform", f"{service}.log").read_text(encoding="utf-8")
                == "ready\n"
            )


def test_product_services_are_rejected_for_platform_group() -> None:
    with removable_temp_dir() as active_root:
        with pytest.raises(ValueError, match="Unknown platform service"):
            ActiveSessionHandler(service="backend", group="platform", active_root=active_root)


def test_unknown_service_is_rejected() -> None:
    with removable_temp_dir() as active_root:
        with pytest.raises(ValueError, match="Unknown product service"):
            ActiveSessionHandler(service="../../etc/passwd", active_root=active_root)


def test_unknown_group_is_rejected() -> None:
    with removable_temp_dir() as active_root:
        with pytest.raises(ValueError, match="Unknown active-session group"):
            ActiveSessionHandler(service=SERVICE, group="admin", active_root=active_root)


def test_concurrent_writes_do_not_interleave_lines() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        line = "x" * 100
        try:
            handler.start_session()
            threads = [
                threading.Thread(target=lambda: [logger.info(line) for _ in range(20)])
                for _ in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            content = product_path(active_root).read_text(encoding="utf-8")
            assert len(content.splitlines()) == 160
            assert all(entry == line for entry in content.splitlines())
        finally:
            logger.handlers.clear()
            handler.close()


def test_barrier_old_emit_cannot_survive_new_session_truncate() -> None:
    with removable_temp_dir() as active_root:
        logger, handler = make_logger(active_root)
        barrier = threading.Barrier(2)
        try:
            handler.start_session()
            logger.info("old-a")

            def worker() -> None:
                barrier.wait()
                logger.info("old-late")

            thread = threading.Thread(target=worker)
            thread.start()
            barrier.wait()
            handler.start_session()
            thread.join()
            content = product_path(active_root).read_text(encoding="utf-8")
            assert "old-a" not in content
            assert content in ("", "old-late\n")
        finally:
            logger.handlers.clear()
            handler.close()


def test_end_session_flush_failure_closes_stream_and_re_raises() -> None:
    with removable_temp_dir() as active_root:
        handler = ActiveSessionHandler(service=SERVICE, active_root=active_root)
        try:
            handler.start_session()
            fake = FailingStream()
            handler._close_stream()
            handler._stream = fake
            with pytest.raises(OSError, match="disk full"):
                handler.end_session()
            assert fake.close_called
            assert handler.state == INACTIVE
            handler.start_session()
            assert handler.state == ACTIVE
        finally:
            handler.close()


def test_run_stream_normalizes_lines_with_redaction() -> None:
    with removable_temp_dir() as active_root:
        collector = PlatformCollector(active_root=active_root)
        try:
            collector.run_stream(
                [
                    "evt=room-created room=main",
                    "error=connection refused",
                    "token=super-secret",
                    "ignored",
                ],
                service="livekit",
            )
            content = (
                active_root.joinpath("platform", "livekit.log")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            assert content[0] == "evt=room-created room=main"
            assert content[1] == "error=connection"
            assert content[2] == "error=ignored"
            assert "super-secret" not in " ".join(content)
        finally:
            collector.close()


def test_cli_help_succeeds() -> None:
    module = "backend.observability.logging.platform_collector"
    result = run_collector(module, ["--help"], "")
    assert result.returncode == 0
    assert "--service" in result.stdout
    assert "--active-root" in result.stdout


def test_cli_writes_platform_log_and_redacts() -> None:
    module = "backend.observability.logging.platform_collector"
    with removable_temp_dir() as active_root:
        payload = "evt=room-ready token=SECRET_VALUE\nerror=boom\n"
        result = run_collector(
            module,
            ["--service", "livekit", "--active-root", str(active_root)],
            payload,
        )
        assert result.returncode == 0
        content = (
            active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8").splitlines()
        )
        assert content == ["evt=room-ready token=[REDACTED]", "error=boom"]
        assert "SECRET_VALUE" not in " ".join(content)


def test_cli_rejects_unknown_service_and_exits_cleanly() -> None:
    module = "backend.observability.logging.platform_collector"
    with removable_temp_dir() as active_root:
        result = run_collector(
            module,
            ["--service", "bogus", "--active-root", str(active_root)],
            "evt=x\n",
        )
        assert result.returncode == 1
        assert "Unknown platform service" in result.stderr


def test_normalize_event_line_drops_unsafe_fields() -> None:
    assert normalize_event_line("evt=ok message=customer-data") == "evt=ok"
    assert normalize_event_line("data=raw") == "error=data=raw"
    assert normalize_event_line("plain text") == "error=plain text"
    assert normalize_event_line("evt=x api_key=hunter2") == "evt=x api_key=[REDACTED]"


def test_flush_failure_closes_stream_and_deactivates() -> None:
    with removable_temp_dir() as active_root:
        handler = ActiveSessionHandler(service=SERVICE, active_root=active_root)
        try:
            handler.start_session()
            fake = FailingStream()
            handler._close_stream()
            handler._stream = fake
            handler.emit(make_record("boom"))
            assert fake.close_called
            assert fake.closed
            assert handler.state == INACTIVE
        finally:
            handler.close()


def test_platform_collector_writes_observed_event_to_exact_platform_log() -> None:
    with removable_temp_dir() as active_root:
        collector = PlatformCollector(active_root=active_root)
        try:
            collector.emit_event("livekit", "room-created")
            collector.emit_event("postgres", "connection-accepted")
            assert (
                active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8")
                == "room-created\n"
            )
            assert (
                active_root.joinpath("platform", "postgres.log").read_text(encoding="utf-8")
                == "connection-accepted\n"
            )
            assert not active_root.joinpath("platform", "backend.log").exists()
        finally:
            collector.close()


def test_platform_collector_rejects_unknown_service_and_closes_idempotently() -> None:
    with removable_temp_dir() as active_root:
        collector = PlatformCollector(active_root=active_root)
        try:
            collector.emit_event("livekit", "ready")
            with pytest.raises(ValueError, match="Unknown platform service"):
                collector.emit_event("backend", "boom")
            collector.close()
            collector.close()
            assert (
                active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8")
                == "ready\n"
            )
        finally:
            collector.close()
