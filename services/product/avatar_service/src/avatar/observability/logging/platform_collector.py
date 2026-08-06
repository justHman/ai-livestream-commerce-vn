"""Safe product-side collection of classified platform events."""

from __future__ import annotations

import argparse
import logging
import math
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Iterable, TextIO

from avatar.observability.logging.active_session_handler import ActiveSessionHandler
from avatar.observability.logging.daily_handler import DailyHandler
from avatar.observability.logging.filters import LevelFilter, StructuredFieldsFilter
from avatar.observability.logging.formatter import ContextFormatter

_PLATFORM_SERVICES = frozenset({"livekit", "lmcache", "postgres", "redis"})
_LATENCY_PATTERN = re.compile(r"^(?:0|[1-9][0-9]{0,7})(?:\.[0-9]{1,3})?$")
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|auth(?:orization)?|body|cookie|credential|customer|data|jwt|"
    r"message|password|payload|prompt|secret|token)(?:_|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:^bearer(?:$|[._:-])|^(?:sk|pk|rk)[-_][a-z0-9]|"
    r"(?:api[_-]?key|secret|token)[:=])"
)
PLATFORM_EVENT_FIELD = "evt=platform_event"
PLATFORM_ERROR_FIELD = "evt=platform_error"
SENSITIVE_DROPPED_FIELD = "evt=sensitive_field_dropped"
UNKNOWN_EVENT_FIELD = "evt=unknown_event"
UNSTRUCTURED_DROPPED_FIELD = "evt=unstructured_line_dropped"
CLI_INVALID_ARGUMENTS_MESSAGE = "platform-collector: invalid arguments"
CLI_COMMAND_FAILED_MESSAGE = "platform-collector: command failed"
CLI_COMMAND_TIMEOUT_MESSAGE = "platform-collector: command timed out"
_READER_POLL_SECONDS = 0.05
_CLEANUP_TIMEOUT_SECONDS = 2.0


class ProcessCleanupError(RuntimeError):
    """Process tree cleanup could not be verified."""


class SanitizedArgumentParser(argparse.ArgumentParser):
    """Argument parser that never reflects malformed caller input."""

    def error(self, _message: str) -> None:
        self.exit(2, f"{CLI_INVALID_ARGUMENTS_MESSAGE}\n")


class PlatformCollector:
    """Own platform handlers and log classified events to platform files."""

    def __init__(
        self,
        active_root: Path | str = ".runtime/logs/active-sessions",
        daily_root: Path | str | None = None,
        retention_days: int = 30,
    ) -> None:
        self._active_root = Path(active_root)
        self._daily_root = (
            Path(daily_root) if daily_root is not None else self._active_root.parent / "daily"
        )
        self._retention_days = retention_days
        self._handlers: dict[str, ActiveSessionHandler] = {}
        self._daily_handlers: dict[str, DailyHandler] = {}
        # RLock: start_session/_handler_for nest under this lock while emit
        # re-enters through handler.handle(); a plain Lock self-deadlocks.
        self._lock = threading.RLock()

    @property
    def active_root(self) -> Path:
        return self._active_root

    @property
    def daily_root(self) -> Path:
        return self._daily_root

    def start_session(self, service: str) -> None:
        """Validate the service and open/reactivate its active log (truncates).

        Every explicit start re-activates the handler: a cached handler left
        inactive by a prior end_session is restarted (truncating the file), so
        a new session after an ended one still writes.
        """
        _validate_service(service)
        with self._lock:
            handler = self._handler_for(service)
            handler.start_session()

    def emit_event(self, service: str, message: str, *, level: int = logging.INFO) -> None:
        """Classify one event without leaking handler-controlled field values."""
        normalized = normalize_event_line(message)
        if normalized is not None:
            self._write(service, normalized, level=level)

    def run_stream(self, lines: Iterable[str], *, service: str = "livekit") -> None:
        """Consume event lines without copying unapproved fields or raw content."""
        handler = self._handler_for(service)
        daily = self._daily_handler_for(service)
        handler.start_session()
        try:
            for line in lines:
                normalized = normalize_event_line(line)
                if normalized is not None:
                    record = make_platform_record(normalized)
                    handler.emit(record)
                    daily.emit(record)
        finally:
            handler.end_session()

    def end_session(self) -> None:
        """End the session on every owned platform handler."""
        with self._lock:
            handlers = list(self._handlers.values())
        for handler in handlers:
            handler.end_session()

    def retain_daily(self, service: str, days: int | None = None) -> list[Path]:
        """Run retention on one platform service's daily view."""
        _validate_service(service)
        return self._daily_handler_for(service).retain(days=days)

    def close(self) -> None:
        """Close every owned platform handler."""
        with self._lock:
            handlers = list(self._handlers.values())
            self._handlers.clear()
            daily_handlers = list(self._daily_handlers.values())
            self._daily_handlers.clear()
        for handler in handlers:
            handler.close()
        for handler in daily_handlers:
            handler.close()

    def _handler_for(self, service: str) -> ActiveSessionHandler:
        _validate_service(service)
        with self._lock:
            handler = self._handlers.get(service)
            if handler is None:
                handler = ActiveSessionHandler(
                    service=service, group="platform", active_root=self._active_root
                )
                # Auto-start so emit_event/run_stream write without a manual
                # start_session (matches the pre-801a0a6 lazy-create behavior).
                handler.start_session()
                handler.setFormatter(ContextFormatter(service=service, colorize=False))
                handler.addFilter(LevelFilter())
                handler.addFilter(StructuredFieldsFilter())
                self._handlers[service] = handler
            return handler

    def _daily_handler_for(self, service: str) -> DailyHandler:
        _validate_service(service)
        with self._lock:
            handler = self._daily_handlers.get(service)
            if handler is None:
                handler = DailyHandler(
                    service=service,
                    group="platform",
                    daily_root=self._daily_root,
                    retention_days=self._retention_days,
                )
                handler.setFormatter(ContextFormatter(service=service, colorize=False))
                handler.addFilter(LevelFilter())
                handler.addFilter(StructuredFieldsFilter())
                self._daily_handlers[service] = handler
            return handler

    def _write(self, service: str, normalized: str, *, level: int) -> None:
        record = make_platform_record(normalized, level=level)
        self._handler_for(service).handle(record)
        self._daily_handler_for(service).handle(record)


def _validate_service(service: str) -> None:
    if service not in _PLATFORM_SERVICES:
        raise ValueError("Unknown platform service")


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return _SENSITIVE_KEY_PATTERN.search(normalized) is not None


def _is_sensitive_value(value: str) -> bool:
    return _SECRET_VALUE_PATTERN.search(value) is not None


def _safe_latency(value: str) -> str | None:
    if not _LATENCY_PATTERN.fullmatch(value):
        return None
    latency = float(value)
    return value if math.isfinite(latency) and latency <= 86_400_000 else None


def normalize_event_line(line: str) -> str | None:
    """Map caller data to finite classifications plus allowlisted provider/latency values."""
    stripped = line.strip()
    if not stripped:
        return None

    has_assignment = False
    has_event = False
    has_error = False
    has_sensitive = False
    provider: str | None = None
    latency: str | None = None

    for part in stripped.split():
        if "=" not in part:
            continue
        has_assignment = True
        key, _, value = part.partition("=")
        if _is_sensitive_key(key) or _is_sensitive_value(value):
            has_sensitive = True
        elif key == "evt" and value:
            has_event = True
        elif key == "error" and value:
            has_error = True
        elif key == "provider" and value in _PLATFORM_SERVICES:
            provider = value
        elif key == "latency_ms":
            latency = _safe_latency(value)

    fields: list[str] = []
    if has_event:
        fields.append(PLATFORM_EVENT_FIELD)
    if has_error:
        fields.append(PLATFORM_ERROR_FIELD)
    if has_sensitive:
        fields.append(SENSITIVE_DROPPED_FIELD)
    if provider is not None:
        fields.append(f"provider={provider}")
    if latency is not None:
        fields.append(f"latency_ms={latency}")
    if fields:
        return " ".join(fields)
    return UNKNOWN_EVENT_FIELD if has_assignment else UNSTRUCTURED_DROPPED_FIELD


def make_platform_record(message: str, *, level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord("platform", level, __file__, 1, message, (), None)


def _drain(
    stream: TextIO,
    collector: PlatformCollector,
    service: str,
    errors: queue.SimpleQueue[BaseException],
) -> None:
    try:
        for line in stream:
            collector.emit_event(service, line)
    except BaseException as error:
        errors.put(error)


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _reader_error(errors: queue.SimpleQueue[BaseException]) -> BaseException | None:
    try:
        return errors.get_nowait()
    except queue.Empty:
        return None


def _wait_for_process(
    process: subprocess.Popen[str],
    errors: queue.SimpleQueue[BaseException],
    deadline: float | None,
    timeout: float | None,
) -> int:
    while True:
        error = _reader_error(errors)
        if error is not None:
            raise error
        if deadline is not None and _remaining(deadline) == 0:
            raise subprocess.TimeoutExpired("platform command", timeout)
        wait_for = _READER_POLL_SECONDS
        if deadline is not None:
            wait_for = min(wait_for, _remaining(deadline))
        try:
            return process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            continue


def _cleanup_error(status: int | None = None) -> ProcessCleanupError:
    suffix = "" if status is None else f" (status={status})"
    return ProcessCleanupError(f"platform process cleanup failed{suffix}")


def _create_windows_job() -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateJobObjectW(None, None)
    if not handle:
        raise _cleanup_error(ctypes.get_last_error())
    return int(handle)


def _assign_windows_job(job: int, process: subprocess.Popen[str]) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.AssignProcessToJobObject(job, int(process._handle)):
        raise _cleanup_error(ctypes.get_last_error())


def _terminate_tree_fallback(
    process: subprocess.Popen[str], deadline: float
) -> ProcessCleanupError | None:
    """Windows cleanup after AssignProcessToJobObject failure: taskkill /T /F.

    taskkill is bounded by the cleanup deadline; its stdout/stderr are discarded
    so neither argv nor raw output ever escapes. returncode 0 (tree terminated)
    and 128 (process already gone) pass; any other status, timeout, or platform
    error surfaces a sanitized ProcessCleanupError.
    """
    if os.name != "nt":
        return _cleanup_error()
    wait_for = _remaining(deadline)
    if wait_for == 0:
        return _cleanup_error()
    try:
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            timeout=wait_for,
        )
    except subprocess.TimeoutExpired:
        return _cleanup_error()
    except OSError as error:
        return _cleanup_error(error.errno)
    if completed.returncode not in (0, 128):
        return _cleanup_error(completed.returncode)
    return None


def _terminate_windows_job(job: int, deadline: float) -> None:
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.TerminateJobObject(job, 1):
        raise _cleanup_error(ctypes.get_last_error())
    milliseconds = max(1, math.ceil(_remaining(deadline) * 1000))
    status = kernel32.WaitForSingleObject(job, milliseconds)
    if status != 0:
        raise _cleanup_error(status)


def _close_windows_job(job: int | None) -> ProcessCleanupError | None:
    if job is None:
        return None
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if kernel32.CloseHandle(job):
        return None
    return _cleanup_error(ctypes.get_last_error())


def _terminate_process_tree(
    process: subprocess.Popen[str], deadline: float, windows_job: int | None
) -> None:
    if os.name == "nt":
        if windows_job is None:
            raise _cleanup_error()
        _terminate_windows_job(windows_job, deadline)
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as error:
        raise _cleanup_error(error.errno) from None


def _join_readers(threads: list[threading.Thread], deadline: float) -> bool:
    for thread in threads:
        thread.join(timeout=_remaining(deadline))
    return all(not thread.is_alive() for thread in threads)


def _close_pipes(process: subprocess.Popen[str]) -> ProcessCleanupError | None:
    failed = False
    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None and not stream.closed:
                stream.close()
        except OSError:
            failed = True
    return _cleanup_error() if failed else None


def _reap_direct_child(
    process: subprocess.Popen[str], deadline: float
) -> ProcessCleanupError | None:
    if process.poll() is None and _remaining(deadline) == 0:
        return _cleanup_error()
    try:
        process.wait(timeout=_remaining(deadline))
    except (OSError, subprocess.SubprocessError):
        return _cleanup_error()
    return None if process.poll() is not None else _cleanup_error()


def _finalize_process(
    process: subprocess.Popen[str],
    threads: list[threading.Thread],
    must_terminate: bool,
    windows_job: int | None,
) -> ProcessCleanupError | None:
    deadline = time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
    failure: ProcessCleanupError | None = None

    if not must_terminate:
        for thread in threads:
            thread.join(timeout=_READER_POLL_SECONDS)
        must_terminate = any(thread.is_alive() for thread in threads)
    if must_terminate:
        try:
            _terminate_process_tree(process, deadline, windows_job)
        except ProcessCleanupError as error:
            failure = error
            try:
                if process.poll() is None:
                    process.kill()
            except OSError:
                pass

    failure = failure or _reap_direct_child(process, deadline)
    readers_stopped = _join_readers(threads, deadline)
    failure = failure or _close_pipes(process)
    if not readers_stopped:
        _join_readers(threads, deadline)
    if process.poll() is None or any(thread.is_alive() for thread in threads):
        failure = failure or _cleanup_error()
    return failure or _close_windows_job(windows_job)


def _validate_run(command: list[str], service: str, timeout: float | None) -> None:
    _validate_service(service)
    if not isinstance(command, list) or not command or not command[0]:
        raise ValueError("invalid command")
    if any(not isinstance(argument, str) for argument in command):
        raise ValueError("invalid command")
    if timeout is not None and (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("invalid timeout")


def run_command(
    command: list[str],
    *,
    service: str,
    active_root: Path | str = ".runtime/logs/active-sessions",
    timeout: float | None = None,
) -> int:
    """Run local argv and collect stdout/stderr under bounded tree cleanup."""
    _validate_run(command, service, timeout)
    collector = PlatformCollector(active_root=active_root)
    collector.start_session(service)
    deadline = time.monotonic() + timeout if timeout is not None else None
    process: subprocess.Popen[str] | None = None
    windows_job: int | None = None
    threads: list[threading.Thread] = []
    errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
    primary: BaseException | None = None
    returncode = 1

    try:
        options: dict[str, object] = {"start_new_session": True}
        if os.name == "nt":
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
            windows_job = _create_windows_job()
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
                **options,
            )
        except BaseException:
            close_error = _close_windows_job(windows_job)
            windows_job = None
            if close_error is not None:
                raise close_error from None
            raise
        if windows_job is not None:
            try:
                _assign_windows_job(windows_job, process)
            except BaseException as assign_error:
                deadline = time.monotonic() + _CLEANUP_TIMEOUT_SECONDS
                fallback_error: ProcessCleanupError | None = None
                if os.name == "nt":
                    fallback_error = _terminate_tree_fallback(process, deadline)
                    if process.poll() is None:
                        try:
                            process.kill()
                        except OSError:
                            fallback_error = fallback_error or _cleanup_error()
                close_error = _close_windows_job(windows_job)
                windows_job = None
                process = None
                if fallback_error is not None:
                    raise fallback_error from None
                if close_error is not None:
                    raise close_error from None
                raise assign_error
        threads = [
            threading.Thread(
                target=_drain,
                args=(stream, collector, service, errors),
                name=f"platform-collector-{name}",
            )
            for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
            if stream is not None
        ]
        for thread in threads:
            thread.start()
        returncode = _wait_for_process(process, errors, deadline, timeout)
    except BaseException as error:
        primary = error
    finally:
        if process is not None:
            cleanup_error = _finalize_process(process, threads, primary is not None, windows_job)
            windows_job = None
            if cleanup_error is not None:
                primary = cleanup_error
        elif windows_job is not None:
            primary = _close_windows_job(windows_job) or primary
        reader_error = _reader_error(errors)
        if primary is None and reader_error is not None:
            primary = reader_error
        try:
            collector.end_session()
        except BaseException as error:
            primary = primary or error
        try:
            collector.close()
        except BaseException as error:
            primary = primary or error

    if primary is not None:
        raise primary
    return returncode


def build_parser() -> argparse.ArgumentParser:
    parser = SanitizedArgumentParser(prog="platform-collector")
    parser.add_argument("--service", default="livekit")
    parser.add_argument("--active-root", default=".runtime/logs/active-sessions")
    subparsers = parser.add_subparsers(dest="subcommand", parser_class=SanitizedArgumentParser)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--timeout", type=float, default=None)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None, *, stdin: TextIO = sys.stdin) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            print(CLI_INVALID_ARGUMENTS_MESSAGE, file=sys.stderr)
            return 1
        try:
            return run_command(
                command,
                service=args.service,
                active_root=args.active_root,
                timeout=args.timeout,
            )
        except subprocess.TimeoutExpired:
            print(CLI_COMMAND_TIMEOUT_MESSAGE, file=sys.stderr)
            return 1
        except Exception:
            print(CLI_COMMAND_FAILED_MESSAGE, file=sys.stderr)
            return 1

    collector = PlatformCollector(active_root=args.active_root)
    try:
        collector.run_stream(stdin, service=args.service)
    except Exception:
        print(CLI_COMMAND_FAILED_MESSAGE, file=sys.stderr)
        return 1
    finally:
        collector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
