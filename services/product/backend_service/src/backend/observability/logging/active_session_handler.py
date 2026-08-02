"""Active-session handler.

Writes to ``{log_dir}/{service}.log``, truncating on first write each
session start.  This is a minimal implementation — the full session-id
file and truncation semantics (Task 1.7) are **not** implemented here.
"""

from __future__ import annotations

import logging
from pathlib import Path


class ActiveSessionHandler(logging.Handler):
    """Write to a single named file, truncated on first emission.

    Parameters
    ----------
    log_dir
        Base directory for logs.
    service
        Service name used as the filename stem.
    level
        Logging level threshold.
    """

    def __init__(
        self,
        log_dir: str,
        service: str,
        level: int = logging.NOTSET,
    ) -> None:
        super().__init__(level)
        self._path = Path(log_dir) / f"{service}.log"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Truncate on first write (session start).
        self._truncated = False

    def emit(self, record: logging.LogRecord) -> None:
        if not self._truncated:
            self._path.write_text("")
            self._truncated = True
        try:
            msg = self.format(record)
            with self._path.open("a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            self.handleError(record)
