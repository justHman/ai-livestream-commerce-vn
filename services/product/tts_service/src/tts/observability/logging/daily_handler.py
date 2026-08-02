"""Daily rotating file handler.

Creates one file per day at ``{log_dir}/{service}/{YYYY-MM-DD}.log``
and applies ``LOG_RETENTION_DAYS`` cleanup on initialization.
"""

from __future__ import annotations

import logging
import time
from datetime import date, timedelta
from pathlib import Path


class DailyHandler(logging.Handler):
    """Rotate on date change, not size.

    On init, files older than *retention_days* are removed.
    """

    def __init__(
        self,
        log_dir: str,
        service: str,
        retention_days: int = 30,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level)
        self._log_dir = Path(log_dir)
        self._service = service
        self._retention_days = retention_days
        self._dir = self._log_dir / service
        self._dir.mkdir(parents=True, exist_ok=True)
        self._rotate_at = time.time()
        self._cleanup()
        self._open()

    def _open(self) -> None:
        fname = date.today().isoformat() + ".log"
        path = self._dir / fname
        self._stream = open(path, "a", encoding="utf-8")

    def _cleanup(self) -> None:
        cutoff = date.today() - timedelta(days=self._retention_days)
        for fpath in self._dir.glob("*.log"):
            try:
                fdate = date.fromisoformat(fpath.stem)
            except (ValueError, IndexError):
                continue
            if fdate < cutoff:
                try:
                    fpath.unlink()
                except OSError:
                    pass

    def emit(self, record: logging.LogRecord) -> None:
        if date.today().isoformat() != date.fromtimestamp(record.created).isoformat():
            self._stream.close()
            self._open()
        try:
            msg = self.format(record)
            self._stream.write(msg + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        if hasattr(self, "_stream"):
            self._stream.close()
        super().close()
