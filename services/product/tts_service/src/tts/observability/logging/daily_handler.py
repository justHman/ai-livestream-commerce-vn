"""UTC daily rotation and retention for service log files.

Task 1.8 owns this module: a logging.Handler appending to
`{daily_root}/{group}/{service}/YYYY-MM-DD.log` where the date is the UTC day
of emission, always derived through ``astimezone(timezone.utc).date()``. The
active stream rotates when the UTC date changes; retention runs atomically on
the first open and on every UTC-day transition and deletes only direct,
non-symlinked, strictly-stale files inside the exact resolved service
directory. Service and group come from fixed allowlists; no session identifier
ever appears in a daily path, and no candidate outside the resolved service
directory can be deleted.
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
_MAX_RETENTION_DAYS = 3650


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
        if clock is None:
            self._clock: Callable[[], datetime] = lambda: datetime.now(_UTC)
        else:
            self._clock = clock
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
            raise ValueError("LOG_RETENTION_DAYS must be a bounded integer")
        if not 1 <= retention_days <= _MAX_RETENTION_DAYS:
            raise ValueError(
                f"LOG_RETENTION_DAYS must be between 1 and {_MAX_RETENTION_DAYS}"
            )
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
        now = self._clock()
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("logging clock must return an aware UTC datetime")
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
        window (`file_date < today - window`), is a direct child of the exact
        resolved service directory, is a regular file (not a symlink), and has
        a ``YYYY-MM-DD.log`` name. Symlinked directories and candidates are
        rejected — the resolved service directory must live under the resolved
        daily root, ensuring no external path is ever unlinked.
        """
        window = self._retention_days if days is None else days
        if (
            not isinstance(window, int)
            or isinstance(window, bool)
            or not 1 <= window <= _MAX_RETENTION_DAYS
        ):
            raise ValueError(
                f"LOG_RETENTION_DAYS must be between 1 and {_MAX_RETENTION_DAYS}"
            )
        cutoff = (today or self.utc_day) - timedelta(days=window)
        directory = self._service_directory()
        if not directory.is_dir():
            return []
        deleted: list[Path] = []
        for path in sorted(directory.glob("*.log")):
            if path.is_symlink() or not path.is_file():
                continue
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

    def _service_directory(self) -> Path:
        root = self._root.resolve()
        directory = (self._root / self._group / self._service).resolve()
        expected = root / self._group / self._service
        # Reject a symlinked group or service component: resolve() yields the
        # real target, which must still be the literal expected path under the
        # resolved root. Any redirect therefore fails containment and no
        # external file can be opened or deleted.
        if directory != expected:
            raise ValueError(f"daily service path escapes configured root: {directory}")
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError(f"daily service path is not a real directory: {directory}")
        return directory

    def _rotate(self, day: date) -> None:
        self._close_stream()
        directory = self._service_directory()
        directory.mkdir(parents=True, exist_ok=True)
        # Retention runs atomically on first open and each UTC transition,
        # reusing the already-read UTC day so the clock is read once per emit.
        self.retain(today=day)
        target = directory / f"{day.isoformat()}.log"
        if target.is_symlink():
            raise ValueError(f"refusing to open symlinked log: {target}")
        self._stream = target.open("a", encoding="utf-8")
        self._current_day = day

    def _close_stream(self) -> None:
        if self._stream is not None and not self._stream.closed:
            self._stream.close()
        self._stream = None