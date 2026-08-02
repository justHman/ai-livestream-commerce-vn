"""Product-side platform log collector for Task 1.7.

Owns ActiveSessionHandler instances for platform services and exposes
normalized event logging, plus an executable entrypoint that reads normalized
event lines from stdin (or any iterable) and writes safe events to platform
log files. Lives in the product observability package (not platform source)
and uses no shared runtime package; platform services need no log-writing
code of their own.
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import threading
from pathlib import Path
from typing import Iterable, TextIO

from llm.observability.logging.active_session_handler import ActiveSessionHandler

SAFE_FIELD_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|auth(?:orization)?|cookie|credential|password|secret|token)(?:_|$)",
    re.IGNORECASE,
)
_CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x1f\x7f]")

EVT_FIELD = "evt"
ERROR_FIELD = "error"


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
        """Consume normalized event lines and write safe events to the platform log.

        Each line is a `key=value` list; unknown or malformed fields are
        dropped, sensitive values are redacted, and control characters are
        removed. Lines without `evt=`/`error=` are normalized as `error=...`
        (safe lines) rather than logged verbatim.
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


def normalize_event_line(line: str) -> str | None:
    """Return a safe, normalized event line or None when nothing is usable."""
    stripped = line.strip()
    if not stripped:
        return None
    fields: list[str] = []
    redacted = False
    for part in stripped.split():
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        if not SAFE_FIELD_PATTERN.match(key) or key in {"message", "msg", "data"}:
            continue
        if _SENSITIVE_KEY_PATTERN.search(key):
            value = _REDACTED
            redacted = True
        if _CONTROL_CHARACTER_PATTERN.search(value) or len(value) > 512:
            value = _REDACTED
            redacted = True
        fields.append(f"{key}={value}")
    if EVT_FIELD not in {f.split("=", 1)[0] for f in fields} and ERROR_FIELD not in {
        f.split("=", 1)[0] for f in fields
    }:
        if redacted:
            return None
        safe = _CONTROL_CHARACTER_PATTERN.sub("", stripped)
        safe = safe if len(safe) <= 512 else safe[:512]
        if not safe:
            return None
        fields.append(f"error={safe}")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="platform-collector",
        description=(
            "Read normalized platform event lines from stdin and write them to "
            "{active_root}/platform/<service>.log. Raw lines are never written "
            "verbatim; only validated key=value fields are kept."
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
    return parser


def main(argv: list[str] | None = None, *, stdin: TextIO = sys.stdin) -> int:
    args = build_parser().parse_args(argv)
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
