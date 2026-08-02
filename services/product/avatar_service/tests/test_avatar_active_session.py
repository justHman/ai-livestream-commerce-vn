from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import pytest

from avatar.observability.logging.active_session_handler import (
    ACTIVE,
    CLOSED,
    INACTIVE,
    ActiveSessionHandler,
)
from avatar.observability.logging.platform_collector import (
    PlatformCollector,
    normalize_event_line,
)

SERVICE = "avatar"


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
    """Stream with independently controlled flush and close failures."""

    def __init__(self, *, fail_flush: bool = True, fail_close: bool = False) -> None:
        self.closed = False
        self.close_called = False
        self._fail_flush = fail_flush
        self._fail_close = fail_close

    def write(self, _text: str) -> None:
        raise OSError("disk full")

    def flush(self) -> None:
        if self._fail_flush:
            raise OSError("disk full")

    def close(self) -> None:
        self.close_called = True
        self.closed = True
        if self._fail_close:
            raise OSError("close also fails")


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
            ActiveSessionHandler(service="avatar", group="platform", active_root=active_root)


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


def test_end_session_close_failure_re_raises_and_detaches() -> None:
    with removable_temp_dir() as active_root:
        handler = ActiveSessionHandler(service=SERVICE, active_root=active_root)
        try:
            handler.start_session()
            fake = FailingStream(fail_flush=False, fail_close=True)
            handler._close_stream()
            handler._stream = fake
            with pytest.raises(OSError, match="close also fails"):
                handler.end_session()
            assert handler._stream is None
            assert handler.state == INACTIVE
        finally:
            handler.close()


def test_end_session_flush_and_close_both_raise_keeps_flush_primary() -> None:
    with removable_temp_dir() as active_root:
        handler = ActiveSessionHandler(service=SERVICE, active_root=active_root)
        try:
            handler.start_session()
            fake = FailingStream(fail_close=True)
            handler._close_stream()
            handler._stream = fake
            with pytest.raises(OSError, match="disk full") as raised:
                handler.end_session()
            assert raised.value.__notes__ == [
                "Secondary stream close failure: OSError: close also fails"
            ]
            assert handler._stream is None
            assert handler.state == INACTIVE
        finally:
            handler.close()


def test_run_stream_keeps_only_semantic_allowlist() -> None:
    with removable_temp_dir() as active_root:
        collector = PlatformCollector(active_root=active_root)
        try:
            collector.run_stream(
                [
                    "evt=room_created provider=livekit latency_ms=12.5 room=main status=ok",
                    "error=ECONNREFUSED customer_id=vip token=Bearer.secret",
                    'data={"customer":"vip","authorization":"Bearer abc123"}',
                    '{"customer":"vip","api_key":"sk-abcdef"}',
                    "ignored raw customer vip bearer sk-abcdef",
                ],
                service="livekit",
            )
            content = (
                active_root.joinpath("platform", "livekit.log")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            joined = " ".join(content)
            assert content == [
                "evt=room_created provider=livekit latency_ms=12.5",
                "error=ECONNREFUSED evt=sensitive_field_dropped",
                "evt=sensitive_field_dropped",
                "evt=unstructured_line_dropped",
                "evt=unstructured_line_dropped",
            ]
            assert all(
                secret not in joined.lower() for secret in ("customer", "vip", "bearer", "sk-")
            )
        finally:
            collector.close()


def test_cli_help_succeeds() -> None:
    module = "avatar.observability.logging.platform_collector"
    result = run_collector(module, ["--help"], "")
    assert result.returncode == 0
    assert "--service" in result.stdout
    assert "--active-root" in result.stdout
    assert "run" in result.stdout


def test_cli_writes_platform_log_and_redacts() -> None:
    module = "avatar.observability.logging.platform_collector"
    with removable_temp_dir() as active_root:
        payload = "evt=room_ready token=SECRET_VALUE\nerror=boom\n"
        result = run_collector(
            module,
            ["--service", "livekit", "--active-root", str(active_root)],
            payload,
        )
        assert result.returncode == 0
        content = (
            active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8").splitlines()
        )
        assert content == ["evt=room_ready evt=sensitive_field_dropped", "error=boom"]
        assert "SECRET_VALUE" not in " ".join(content)


def test_cli_rejects_unknown_service_and_exits_cleanly() -> None:
    module = "avatar.observability.logging.platform_collector"
    with removable_temp_dir() as active_root:
        result = run_collector(
            module,
            ["--service", "bogus", "--active-root", str(active_root)],
            "evt=x\n",
        )
        assert result.returncode == 1
        assert result.stderr.strip() == "platform-collector: command failed"


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "evt=ready error=NONE provider=livekit latency_ms=42.125 room=main status=ok",
            "evt=ready error=NONE provider=livekit latency_ms=42.125",
        ),
        ("evt=x api_key=hunter2", "evt=x evt=sensitive_field_dropped"),
        ("evt=x customer_payload=vip", "evt=x evt=sensitive_field_dropped"),
        ("evt=x Authorization=Bearer.secret", "evt=x evt=sensitive_field_dropped"),
        ("evt=Bearer.secret", "evt=sensitive_field_dropped"),
        ("error=sk-abcdef", "evt=sensitive_field_dropped"),
        ("latency_ms=0042", None),
        ("latency_ms=-1", None),
        ("latency_ms=nan", None),
        ("latency_ms=1e3", None),
        ("latency_ms=999999999", None),
        ("provider=livekit/../../secret", None),
        ('data={"customer":"vip"}', "evt=sensitive_field_dropped"),
        ('{"customer":"vip","token":"sk-abcdef"}', "evt=unstructured_line_dropped"),
        ("plain bearer sk-abcdef customer vip", "evt=unstructured_line_dropped"),
        ("", None),
    ],
)
def test_normalize_event_line_accepts_only_semantic_allowlist(
    line: str, expected: str | None
) -> None:
    assert normalize_event_line(line) == expected


def test_run_command_collects_stdout_and_stderr_concurrently() -> None:
    from avatar.observability.logging.platform_collector import run_command

    with removable_temp_dir() as active_root:
        child = (
            "import sys\n"
            "print('evt=ready')\n"
            "print('error=ECONNREFUSED', file=sys.stderr)\n"
            "print('secret=sk-abcdef')\n"
        )
        code = run_command(
            [sys.executable, "-c", child],
            service="livekit",
            active_root=active_root,
        )
        assert code == 0
        content = (
            active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8").splitlines()
        )
        joined = " ".join(content)
        assert "evt=ready" in content
        assert "error=ECONNREFUSED" in content
        assert "sk-abcdef" not in joined


def test_run_command_propagates_nonzero_exit() -> None:
    from avatar.observability.logging.platform_collector import run_command

    with removable_temp_dir() as active_root:
        code = run_command(
            [sys.executable, "-c", "print('evt=dying'); raise SystemExit(3)"],
            service="livekit",
            active_root=active_root,
        )
        assert code == 3
        content = active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8")
        assert "evt=dying" in content


def test_run_command_invalid_service_fails_before_popen(monkeypatch: pytest.MonkeyPatch) -> None:
    from avatar.observability.logging import platform_collector

    def fail_popen(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Popen must not run")

    monkeypatch.setattr(platform_collector.subprocess, "Popen", fail_popen)
    with pytest.raises(ValueError, match="Unknown platform service"):
        platform_collector.run_command([sys.executable, "-c", "pass"], service="bogus")


def test_run_command_timeout_terminates_process_tree() -> None:
    from avatar.observability.logging.platform_collector import run_command

    with removable_temp_dir() as active_root:
        descendant_pid_path = active_root / "descendant.pid"
        grandchild_pid_path = active_root / "grandchild.pid"
        grandchild = (
            "import os,pathlib,subprocess,sys,time; "
            f"path=pathlib.Path({str(grandchild_pid_path)!r}); "
            "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
            "path.write_text(str(child.pid),encoding='ascii'); time.sleep(30)"
        )
        child = (
            "import pathlib, subprocess, sys, time\n"
            f"path = pathlib.Path({str(descendant_pid_path)!r})\n"
            f"descendant = subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
            "path.write_text(str(descendant.pid), encoding='ascii')\n"
            "print('evt=ready', flush=True)\n"
            "time.sleep(30)\n"
        )
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_command(
                [sys.executable, "-c", child],
                service="livekit",
                active_root=active_root,
                timeout=1.0,
            )
        elapsed = time.monotonic() - started
        assert descendant_pid_path.exists() and grandchild_pid_path.exists() and elapsed < 3.0


def test_run_command_reader_exception_terminates_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from avatar.observability.logging import platform_collector

    def fail_normalize(_line: str) -> str | None:
        raise LookupError("reader failed")

    monkeypatch.setattr(platform_collector, "normalize_event_line", fail_normalize)
    started = time.monotonic()
    with removable_temp_dir() as active_root:
        with pytest.raises(LookupError, match="reader failed"):
            platform_collector.run_command(
                [
                    sys.executable,
                    "-c",
                    "import time; print('evt=ready', flush=True); time.sleep(30)",
                ],
                service="livekit",
                active_root=active_root,
                timeout=3.0,
            )
    assert time.monotonic() - started < 3.0


def test_cli_run_timeout_message_contains_no_command_or_exception() -> None:
    module = "avatar.observability.logging.platform_collector"
    private_value = "customer-private-value"
    result = run_collector(
        module,
        [
            "--service",
            "livekit",
            "run",
            "--timeout",
            "0.1",
            "--",
            sys.executable,
            "-c",
            f"import time; value={private_value!r}; time.sleep(30)",
        ],
        "",
    )
    assert result.stderr.strip() == "platform-collector: command timed out"


def test_cli_run_subcommand_launches_child_and_collects() -> None:
    module = "avatar.observability.logging.platform_collector"
    with removable_temp_dir() as active_root:
        payload = "print('evt=ready'); print('error=ECONNREFUSED', file=__import__('sys').stderr)"
        result = run_collector(
            module,
            [
                "--service",
                "livekit",
                "--active-root",
                str(active_root),
                "run",
                "--",
                sys.executable,
                "-c",
                payload,
            ],
            "",
        )
        assert result.returncode == 0
        content = (
            active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8").splitlines()
        )
        assert "evt=ready" in content
        assert "error=ECONNREFUSED" in content


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


def test_platform_collector_writes_only_normalized_events_to_exact_log() -> None:
    with removable_temp_dir() as active_root:
        collector = PlatformCollector(active_root=active_root)
        try:
            collector.emit_event("livekit", "evt=room_created customer=vip")
            collector.emit_event("postgres", "evt=connection_accepted latency_ms=7")
            assert (
                active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8")
                == "evt=room_created evt=sensitive_field_dropped\n"
            )
            assert (
                active_root.joinpath("platform", "postgres.log").read_text(encoding="utf-8")
                == "evt=connection_accepted latency_ms=7\n"
            )
            assert not active_root.joinpath("platform", "avatar.log").exists()
        finally:
            collector.close()


def test_platform_collector_rejects_unknown_service_and_closes_idempotently() -> None:
    with removable_temp_dir() as active_root:
        collector = PlatformCollector(active_root=active_root)
        try:
            collector.emit_event("livekit", "evt=ready")
            with pytest.raises(ValueError, match="Unknown platform service"):
                collector.emit_event("avatar", "boom")
            collector.close()
            collector.close()
            assert (
                active_root.joinpath("platform", "livekit.log").read_text(encoding="utf-8")
                == "evt=ready\n"
            )
        finally:
            collector.close()
