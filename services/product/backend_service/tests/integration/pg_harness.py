"""Embedded / portable PostgreSQL harness for integration tests (Windows-safe).

Downloads the EDB portable PostgreSQL binaries once to ``.runtime/test-pg``
(gitignored), ``initdb``'s a data directory with trust auth + ``--no-locale``,
runs a server on an ephemeral port, and creates/drops isolated databases per
test. No Docker, no native PostgreSQL install, no third-party Python package
(the ``embedded-postgres`` PyPI package is not available).

Usage (session fixture in conftest)::

    server = TestPostgres()
    server.start()
    try:
        yield server
    finally:
        server.stop()
"""

from __future__ import annotations

import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.request import urlretrieve

_PG_VERSION = "16.4"
_URL_TEMPLATE = (
    "https://get.enterprisedb.com/postgresql/postgresql-{ver}-1-windows-x64-binaries.zip"
)
_DOWNLOAD_TIMEOUT_SECONDS = 900  # 338 MB zip
_START_TIMEOUT_SECONDS = 60
_HOST = "127.0.0.1"

# Implementations repo root (this file:
# implementations/services/product/backend_service/tests/integration/).
_REPO_ROOT = Path(__file__).resolve().parents[5]
_CACHE_DIR = _REPO_ROOT / ".runtime" / "test-pg"


def _free_port() -> int:
    """Reserve a free TCP port for the test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_HOST, 0))
        return sock.getsockname()[1]


class TestPostgres:
    """Manages one ephemeral PostgreSQL server for the integration session."""

    __test__ = False  # pytest must not collect this as a test class.

    def __init__(
        self,
        *,
        cache_dir: Path = _CACHE_DIR,
        version: str = _PG_VERSION,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.version = version
        self.port = _free_port()
        self.host = _HOST
        self._bin = None
        self._data_dir = None
        self._log_path = None
        self._started = False

    # ── binaries ──────────────────────────────────────────────────────

    @property
    def bin_dir(self) -> Path:
        if self._bin is None:
            raise RuntimeError("TestPostgres not started")
        return self._bin

    def _ensure_binaries(self) -> None:
        """Download + extract the portable binaries once (idempotent)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        zip_path = self.cache_dir / "pg16-binaries.zip"
        extracted = self.cache_dir / "pg16"
        pgsql = extracted / "pgsql"
        if pgsql.exists() and (pgsql / "bin" / "initdb.exe").exists():
            self._bin = pgsql / "bin"
            return
        if not zip_path.exists():
            url = _URL_TEMPLATE.format(ver=self.version)
            print(f"[pg-harness] downloading {url} -> {zip_path}")
            urlretrieve(url, zip_path)
        print(f"[pg-harness] extracting {zip_path}")
        shutil.unpack_archive(str(zip_path), str(extracted))
        self._bin = pgsql / "bin"

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        """Init + start the server (idempotent)."""
        if self._started:
            return
        self._ensure_binaries()
        # Fresh data dir per port so reruns never collide with stale state.
        self._data_dir = self.cache_dir / f"data-{self.port}"
        self._log_path = self.cache_dir / f"pg-{self.port}.log"
        if self._data_dir.exists():
            shutil.rmtree(self._data_dir)
        self._data_dir.mkdir(parents=True, exist_ok=True)

        initdb = self.bin_dir / "initdb.exe"
        subprocess.run(
            [
                str(initdb),
                "-D",
                str(self._data_dir),
                "-U",
                "postgres",
                "--no-locale",
                "-E",
                "UTF8",
                "--auth=trust",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        pg_ctl = self.bin_dir / "pg_ctl.exe"
        # DEVNULL (not capture_output): pg_ctl spawns the postmaster which
        # inherits pg_ctl's stdio; with a pipe, communicate() waits forever for
        # EOF that never comes while the server runs. Redirect to DEVNULL so
        # the server's stdio is the null device and subprocess.run returns
        # when pg_ctl (the direct child) exits.
        subprocess.run(
            [
                str(pg_ctl),
                "-D",
                str(self._data_dir),
                "-l",
                str(self._log_path),
                "-o",
                f"-p {self.port} -h {self.host} -c listen_addresses={self.host}",
                "start",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        self._wait_ready()
        self._started = True
        print(f"[pg-harness] postgres up on {self.dsn()} (data={self._data_dir.name})")

    def _wait_ready(self) -> None:
        """Wait for the server to accept connections via asyncpg (cheap on
        Windows vs spawning pg_isready.exe repeatedly)."""
        import asyncio

        import asyncpg

        last_error: str | None = None

        async def _probe() -> bool:
            nonlocal last_error
            try:
                conn = await asyncpg.connect(self.dsn("postgres"), timeout=2.0)
            except Exception as exc:  # noqa: BLE001 - recorded for diagnostics
                last_error = f"{type(exc).__name__}: {exc}"
                return False
            await conn.close()
            return True

        deadline = time.monotonic() + _START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if asyncio.run(_probe()):
                return
            time.sleep(0.3)
        log = self._log_path.read_text(errors="ignore") if self._log_path else ""
        raise RuntimeError(
            f"postgres did not become ready on port {self.port}; "
            f"last probe error: {last_error}\n{log[-2000:]}"
        )

    def stop(self) -> None:
        if not self._started:
            return
        pg_ctl = self.bin_dir / "pg_ctl.exe"
        subprocess.run(
            [str(pg_ctl), "-D", str(self._data_dir), "-m", "fast", "stop"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._started = False
        print(f"[pg-harness] postgres stopped (port {self.port})")

    # ── databases ─────────────────────────────────────────────────────

    def dsn(self, dbname: str = "postgres") -> str:
        return f"postgresql://postgres@{self.host}:{self.port}/{dbname}"

    async def create_database(self) -> str:
        """Create an isolated database and return its connection URL."""
        import asyncpg

        name = f"test_{uuid.uuid4().hex[:12]}"
        conn = await asyncpg.connect(self.dsn("postgres"))
        try:
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()
        return self.dsn(name)

    async def drop_database(self, dsn: str) -> None:
        import asyncpg

        name = dsn.rsplit("/", 1)[-1]
        conn = await asyncpg.connect(self.dsn("postgres"))
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await conn.close()
