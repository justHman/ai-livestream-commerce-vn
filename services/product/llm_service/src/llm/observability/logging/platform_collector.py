"""Safe product-side collection of allowlisted platform events."""

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

from llm.observability.logging.active_session_handler import ActiveSessionHandler

_PLATFORM_SERVICES = frozenset({"livekit", "lmcache", "postgres", "redis"})
_CODE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
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
SENSITIVE_DROPPED_FIELD = "evt=sensitive_field_dropped"
UNSTRUCTURED_DROPPED_FIELD = "evt=unstructured_line_dropped"
CLI_COMMAND_FAILED_MESSAGE = "platform-collector: command failed"
CLI_COMMAND_TIMEOUT_MESSAGE = "platform-collector: command timed out"
_READER_POLL_SECONDS = 0.05
_CLEANUP_TIMEOUT_SECONDS = 2.0


class PlatformCollector:
    """Own platform handlers and log normalized events to platform files."""

    def __init__(self, active_root: Path | str = ".runtime/logs/active-sessions") -> None:
        self._active_root = Path(active_root)
        self._handlers: dict[str, ActiveSessionHandler] = {}
        self._lock = threading.Lock()

    @property
    def active_root(self) -> Path:
        return self._active_root

    def emit_event(self, service: str, message: str, *, level: int = logging.INFO) -> None:
        """Normalize one event and write only approved semantic fields."""
        handler = self._handler_for(service)
        normalized = normalize_event_line(message)
        if normalized is not None:
            handler.handle(make_platform_record(normalized, level=level))

    def run_stream(self, lines: Iterable[str], *, service: str = "livekit") -> None:
        """Consume event lines without copying unapproved fields or raw content."""
        handler = self._handler_for(service)
        handler.start_session()
        try:
            for line in lines:
                normalized = normalize_event_line(line)
                if normalized is not None:
                    handler.emit(make_platform_record(normalized))
        finally:
            handler.end_session()

    def end_session(self) -> None:
        """End the session on every owned platform handler."""
        with self._lock:
            handlers = list(self._handlers.values())
        for handler in handlers:
            handler.end_session()

    def close(self) -> None:
        """Close every owned platform handler."""
        with self._lock:
            handlers = list(self._handlers.values())
            self._handlers.clear()
        for handler in handlers:
            handler.close()

    def _handler_for(self, service: str) -> ActiveSessionHandler:
        _validate_service(service)
        with self._lock:
            handler = self._handlers.get(service)
            if handler is not None:
                return handler
            handler = ActiveSessionHandler(
                service=service, group="platform", active_root=self._active_root
            )
            handler.start_session()
            self._handlers[service] = handler
            return handler


def _validate_service(service: str) -> None:
    if service not in _PLATFORM_SERVICES:
        raise ValueError(f"Unknown platform service={service!r}")


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    return _SENSITIVE_KEY_PATTERN.search(normalized) is not None


def _safe_code(value: str) -> str | None:
    if _SECRET_VALUE_PATTERN.search(value):
        return None
    return value if _CODE_PATTERN.fullmatch(value) else None


def _safe_latency(value: str) -> str | None:
    if not _LATENCY_PATTERN.fullmatch(value):
        return None
    latency = float(value)
    return value if math.isfinite(latency) and latency <= 86_400_000 else None


def normalize_event_line(line: str) -> str | None:
    """Keep only safe evt/error/provider codes and numeric latency_ms."""
    stripped = line.strip()
    if not stripped:
        return None
    fields: list[str] = []
    has_assignment = False
    has_sensitive = False
    for part in stripped.split():
        if "=" not in part:
            continue
        has_assignment = True
        key, _, value = part.partition("=")
        if _is_sensitive_key(key):
            has_sensitive = True
        elif key in {"evt", "error", "provider"}:
            safe_value = _safe_code(value)
            if safe_value is not None:
                fields.append(f"{key}={safe_value}")
            elif _SECRET_VALUE_PATTERN.search(value):
                has_sensitive = True
        elif key == "latency_ms":
            safe_value = _safe_latency(value)
            if safe_value is not None:
                fields.append(f"{key}={safe_value}")
    if has_sensitive:
        fields.append(SENSITIVE_DROPPED_FIELD)
    if fields:
        return " ".join(fields)
    return None if has_assignment else UNSTRUCTURED_DROPPED_FIELD


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


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else max(0.0, deadline - time.monotonic())


def _first_reader_error(errors: queue.SimpleQueue[BaseException]) -> BaseException | None:
    try:
        return errors.get_nowait()
    except queue.Empty:
        return None


def _wait_for_process(
    process: subprocess.Popen[str],
    errors: queue.SimpleQueue[BaseException],
    deadline: float | None,
    command: list[str],
    timeout: float | None,
) -> int:
    while True:
        reader_error = _first_reader_error(errors)
        if reader_error is not None:
            raise reader_error
        remaining = _remaining(deadline)
        if remaining == 0:
            raise subprocess.TimeoutExpired(command, timeout)
        wait_for = (
            _READER_POLL_SECONDS if remaining is None else min(_READER_POLL_SECONDS, remaining)
        )
        try:
            return process.wait(timeout=wait_for)
        except subprocess.TimeoutExpired:
            continue


def _create_windows_job() -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job_handle = kernel32.CreateJobObjectW(None, None)
    return int(job_handle) if job_handle else None


def _assign_windows_job(windows_job: int, process: subprocess.Popen[str]) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    return bool(
        kernel32.AssignProcessToJobObject(
            wintypes.HANDLE(windows_job), wintypes.HANDLE(int(process._handle))
        )
    )


def _terminate_process_tree(
    process: subprocess.Popen[str], deadline: float | None, windows_job: int | None
) -> None:
    if os.name == "nt":
        if windows_job is not None:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            if kernel32.TerminateJobObject(wintypes.HANDLE(windows_job), 1):
                return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=_CLEANUP_TIMEOUT_SECONDS,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            if process.poll() is None:
                process.kill()
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _close_windows_job(windows_job: int | None) -> None:
    if windows_job is None:
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle(wintypes.HANDLE(windows_job))


def _close_pipes(process: subprocess.Popen[str]) -> None:
    for stream in (process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()


def _join_readers(threads: list[threading.Thread], deadline: float | None) -> None:
    join_deadline = deadline or (time.monotonic() + _CLEANUP_TIMEOUT_SECONDS)
    for thread in threads:
        thread.join(timeout=_remaining(join_deadline))


def _validate_run(command: list[str], service: str, timeout: float | None) -> None:
    _validate_service(service)
    if not command or not isinstance(command[0], str) or not command[0]:
        raise ValueError("command requires a non-empty executable")
    if any(not isinstance(argument, str) for argument in command):
        raise ValueError("command arguments must be strings")
    if timeout is not None and (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0
    ):
        raise ValueError("timeout must be a positive number")


def run_command(
    command: list[str],
    *,
    service: str,
    active_root: Path | str = ".runtime/logs/active-sessions",
    timeout: float | None = None,
) -> int:
    """Run a local argv command and safely collect stdout/stderr within one deadline."""
    _validate_run(command, service, timeout)
    deadline = time.monotonic() + timeout if timeout is not None else None
    collector = PlatformCollector(active_root=active_root)
    process: subprocess.Popen[str] | None = None
    windows_job: int | None = None
    windows_job_owned = False
    threads: list[threading.Thread] = []
    errors: queue.SimpleQueue[BaseException] = queue.SimpleQueue()
    primary: BaseException | None = None
    returncode = 1
    try:
        popen_options: dict[str, object] = {"start_new_session": True}
        if os.name == "nt":
            popen_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt":
            windows_job = _create_windows_job()
            windows_job_owned = windows_job is not None
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=False,
            **popen_options,
        )
        if windows_job is not None and not _assign_windows_job(windows_job, process):
            _close_windows_job(windows_job)
            windows_job = None
            windows_job_owned = False
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
        returncode = _wait_for_process(process, errors, deadline, command, timeout)
        _join_readers(threads, deadline)
        primary = _first_reader_error(errors)
        if primary is None and any(thread.is_alive() for thread in threads):
            primary = RuntimeError("platform collector reader did not stop")
    except BaseException as error:
        primary = error
    finally:
        cleanup_deadline = deadline or (time.monotonic() + _CLEANUP_TIMEOUT_SECONDS)
        if process is not None:
            needs_termination = primary is not None or any(thread.is_alive() for thread in threads)
            if needs_termination:
                _terminate_process_tree(process, cleanup_deadline, windows_job)
            _close_pipes(process)
            _join_readers(threads, cleanup_deadline)
            if any(thread.is_alive() for thread in threads) and primary is None:
                primary = RuntimeError("platform collector reader did not stop")
            if process.poll() is None:
                try:
                    process.wait(timeout=_remaining(cleanup_deadline))
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process, cleanup_deadline, windows_job)
            if windows_job_owned:
                _close_windows_job(windows_job)
        reader_error = _first_reader_error(errors)
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
    parser = argparse.ArgumentParser(prog="platform-collector")
    parser.add_argument("--service", default="livekit")
    parser.add_argument("--active-root", default=".runtime/logs/active-sessions")
    subparsers = parser.add_subparsers(dest="subcommand")
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--timeout", type=float, default=None)
    run_parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: list[str] | None = None, *, stdin: TextIO = sys.stdin) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand == "run":
        command = args.command[1:] if args.command[:1] == ["--"] else args.command
        if not command:
            print("platform-collector: run requires a command after '--'", file=sys.stderr)
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
        except (ValueError, OSError, RuntimeError):
            print(CLI_COMMAND_FAILED_MESSAGE, file=sys.stderr)
            return 1
    collector = PlatformCollector(active_root=args.active_root)
    try:
        collector.run_stream(stdin, service=args.service)
    except (ValueError, OSError):
        print(CLI_COMMAND_FAILED_MESSAGE, file=sys.stderr)
        return 1
    finally:
        collector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
