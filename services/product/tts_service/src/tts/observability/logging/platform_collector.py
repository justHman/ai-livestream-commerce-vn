"""Product-side platform log collector and command runner for Task 1.7.

Owns ActiveSessionHandler instances for platform services and exposes
normalized event logging, plus an executable entrypoint that either reads
event lines from stdin (or any iterable) or launches a configured upstream
command (argv list, no shell) and drains its stdout/stderr concurrently into
safe platform log events. Lives in the product observability package (not
platform source) and uses no shared runtime package; platform services need
no log-writing code of their own. The CLI is a local operator tool only —
no network exposure.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Iterable, TextIO

from tts.observability.logging.active_session_handler import ActiveSessionHandler

SAFE_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
SAFE_TOKEN_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
SAFE_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.:@/%-]{1,128}$")
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|auth(?:orization)?|cookie|credential|password|secret|token)(?:_|$)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERN = re.compile(
    r"(?i)(?:bearer|secret|api[_-]?key|token|sk[-_][a-z0-9]|pk[-_][a-z0-9]|rk[-_][a-z0-9])"
)

EVT_FIELD = "evt"
ERROR_FIELD = "error"
UNSTRUCTURED_DROPPED_FIELD = "evt=unstructured_line_dropped"


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
        """Write one normalized event to `{active_root}/platform/{service}.log`."""
        handler = self._handler_for(service)
        record = logging.LogRecord(
            name="platform",
            level=level,
            pathname=__file__,
            lineno=1,
            msg=message,
            args=(),
            exc_info=None,
        )
        handler.handle(record)

    def run_stream(self, lines: Iterable[str], *, service: str = "livekit") -> None:
        """Consume event lines and write safe events to the platform log.

        Each line is a `key=value` list; raw or unstructured lines are never
        copied into the log (they emit a constant `evt=unstructured_line_dropped`
        marker instead). Unknown/malformed fields are dropped; sensitive keys
        and secret-looking values (bearer tokens, sk-/pk-/rk- prefixes, API
        keys) are redacted; `error=` values accept only bounded safe tokens.
        """
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
        """End the session on every owned platform handler (retains files)."""
        with self._lock:
            handlers = list(self._handlers.values())
        for handler in handlers:
            handler.end_session()

    def close(self) -> None:
        """Close every owned platform handler (idempotent)."""
        with self._lock:
            handlers = list(self._handlers.values())
            self._handlers.clear()
        for handler in handlers:
            handler.close()

    def _handler_for(self, service: str) -> ActiveSessionHandler:
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


def _safe_value(key: str, value: str) -> str | None:
    """Return a safe value for a field, or None to drop the field."""
    if _SENSITIVE_KEY_PATTERN.search(key):
        return _REDACTED
    if SECRET_VALUE_PATTERN.search(value) or not SAFE_VALUE_PATTERN.match(value):
        return _REDACTED
    return value


def normalize_event_line(line: str) -> str | None:
    """Return a safe, normalized event line or None when nothing is usable."""
    stripped = line.strip()
    if not stripped:
        return None
    fields: list[str] = []
    has_structured = False
    for part in stripped.split():
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if not SAFE_FIELD_PATTERN.match(key) or key in {"message", "msg", "data"}:
            continue
        if key == ERROR_FIELD:
            if SECRET_VALUE_PATTERN.search(value) or not SAFE_TOKEN_PATTERN.match(value):
                fields.append(f"{key}=[REDACTED]")
            else:
                fields.append(f"{key}={value}")
            has_structured = True
            continue
        if key == EVT_FIELD:
            fields.append(f"{key}={value}")
            has_structured = True
            continue
        safe = _safe_value(key, value)
        if safe is not None:
            fields.append(f"{key}={safe}")
    if not has_structured:
        return UNSTRUCTURED_DROPPED_FIELD
    return " ".join(fields) if fields else None


def make_platform_record(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        name="platform",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )


def _drain(stream: TextIO, collector: PlatformCollector, service: str) -> None:
    for line in stream:
        normalized = normalize_event_line(line)
        if normalized is not None:
            collector.emit_event(service, normalized)


def run_command(
    command: list[str],
    *,
    service: str,
    active_root: Path | str = ".runtime/logs/active-sessions",
    timeout: float | None = None,
) -> int:
    """Launch a configured upstream command and collect its output as events.

    Runs the argv list with no shell, drains stdout and stderr concurrently
    (no deadlock), returns the child exit code, and always ends the session
    and closes collectors. Raises subprocess.TimeoutExpired on timeout after
    terminating the child.
    """
    collector = PlatformCollector(active_root=active_root)
    try:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            threads = [
                threading.Thread(target=_drain, args=(stream, collector, service), daemon=True)
                for stream in (process.stdout, process.stderr)
            ]
            for thread in threads:
                thread.start()
            try:
                returncode = process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
                raise
            for thread in threads:
                thread.join()
            return returncode
    finally:
        collector.end_session()
        collector.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform-collector",
        description=(
            "Collect platform events. 'run' launches a configured upstream "
            "command (argv list, no shell) and normalizes its stdout/stderr "
            "into {active_root}/platform/<service>.log. Without 'run', reads "
            "normalized event lines from stdin. Raw lines are never written "
            "verbatim; only validated fields are kept."
        ),
    )
    parser.add_argument(
        "--service",
        default="livekit",
        help="Platform service name; one of livekit|lmcache|postgres|redis (default: livekit)",
    )
    parser.add_argument(
        "--active-root",
        default=".runtime/logs/active-sessions",
        help="Directory holding platform/<service>.log files (default: .runtime/logs/active-sessions)",
    )
    subparsers = parser.add_subparsers(dest="subcommand")
    run_parser = subparsers.add_parser(
        "run", help="Launch an upstream command and collect its output"
    )
    run_parser.add_argument(
        "--timeout", type=float, default=None, help="Optional command timeout in seconds"
    )
    run_parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="Upstream command as an argv list (no shell); prefix with '--'",
    )
    return parser


def main(argv: list[str] | None = None, *, stdin: TextIO = sys.stdin) -> int:
    args = build_parser().parse_args(argv)
    if args.subcommand == "run":
        command = args.command
        if command and command[0] == "--":
            command = command[1:]
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
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            print(f"platform-collector: {error}", file=sys.stderr)
            return 1
    collector = PlatformCollector(active_root=args.active_root)
    try:
        collector.run_stream(stdin, service=args.service)
    except (ValueError, OSError) as error:
        print(f"platform-collector: {error}", file=sys.stderr)
        return 1
    finally:
        collector.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
