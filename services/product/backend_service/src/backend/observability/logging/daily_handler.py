"""UTC daily rotation and retention for service log files.

Task 1.8 owns this module: a logging.Handler appending to
`{daily_root}/{group}/{service}/YYYY-MM-DD.log` where the date is the UTC day
of emission. The active stream rotates when the UTC date changes, and retain()
deletes files that fall outside the configured retention window. Service and
group names come from fixed allowlists; no session identifier ever appears in
a daily path.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, TextIO

_PRODUCT_SERVICES = frozenset({"backend", "llm", "tts", "avatar"})
_PLATFORM_SERVICES = frozenset({"livekit", "lmcache", "postgres", "redis"})
_GROUPS = {"product": _PRODUCT_SERVICES, "platform": _PLATFORM_SERVICES}
_UTC = timezone.utc
_DATE_STEM = re.compile(r"^(\d{4}-\d{2}-\d{2})$")


class DailyHandler(logging.Handler):
    """Append records to one UTC-dated file, rotating at UTC midnight."""

    def __init__(
        self,
        service: str,
        group: str = "product",
        daily_root: Path | str = ".runtime/logs/daily",
        retention_days: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__()
        # Assign every instance attribute before validation so a handler that
        # fails construction can still be closed safely by the logging
        # shutdown registry.
        self._group = group
        self._service = service
        self._root = Path(daily_root)
        self._retention_days = retention_days
        self._clock = clock or (lambda: datetime.now(_UTC))
        self._stream: TextIO | None = None
        self._current_day: date | None = None
        self._closed = False
        services = _GROUPS.get(group)
        if services is None:
            raise ValueError(f"Unknown log group={group!r}; expected product|platform")
        if service not in services:
            raise ValueError(
                f"Unknown {group} service={service!r}; expected one of {sorted(services)}"
            )
        if not isinstance(retention_days, int) or isinstance(retention_days, bool):
            raise ValueError("LOG_RETENTION_DAYS must be a positive integer")
        if retention_days < 1:
            raise ValueError("LOG_RETENTION_DAYS must be a positive integer")
        self.createLock()

    @property
    def group(self) -> str:
        return self._group

    @property
    def service(self) -> str:
        return self._service

    @property
    def daily_root(self) -> Path:
        return self._root

    @property
    def utc_day(self) -> date:
        """UTC date of the current clock reading."""
        now = self._clock()
        return now.astimezone(_UTC).date()

    def emit(self, record: logging.LogRecord) -> None:
        with self.lock:
            if self._closed:
                return
            try:
                today = self.utc_day
                if self._stream is None or today != self._current_day:
                    self._rotate(today)
                self._stream.write(self.format(record) + "\n")  # type: ignore[union-attr]
                self._stream.flush()  # type: ignore[union-attr]
            except Exception:
                self._close_stream()
                self.handleError(record)

    def retain(self, days: int | None = None, *, today: date | None = None) -> list[Path]:
        """Delete expired daily files and return the deleted paths.

        A file is deleted only when it is strictly older than the configured
        window: `file_date < today - days`. The current UTC day and every
        retained day therefore always survive, while exactly-N-days-old files
        are removed. Files whose names are not `YYYY-MM-DD.log` are never
        touched.
        """
        window = self._retention_days if days is None else days
        if not isinstance(window, int) or isinstance(window, bool) or window < 1:
            raise ValueError("LOG_RETENTION_DAYS must be a positive integer")
        cutoff = (today or self.utc_day) - timedelta(days=window)
        directory = self._root / self._group / self._service
        if not directory.is_dir():
            return []
        deleted: list[Path] = []
        for path in sorted(directory.glob("*.log")):
            match = _DATE_STEM.fullmatch(path.stem)
            if match is None:
                continue
            file_date = date.fromisoformat(match.group(1))
            if file_date < cutoff:
                path.unlink()
                deleted.append(path)
        return deleted

    def close(self) -> None:
        """Terminal close; idempotent; emit refused afterwards."""
        with self.lock:
            if self._closed:
                return
            super().close()
            self._close_stream()
            self._closed = True

    def _rotate(self, day: date) -> None:
        self._close_stream()
        directory = self._root / self._group / self._service
        directory.mkdir(parents=True, exist_ok=True)
        self._stream = (directory / f"{day.isoformat()}.log").open("a", encoding="utf-8")
        self._current_day = day
        self.retain(today=day)

    def _close_stream(self) -> None:
        if self._stream is not None and not self._stream.closed:
            self._stream.close()
        self._stream = None
