"""Embedded / portable PostgreSQL harness for integration tests.

Windows: downloads the EDB portable PostgreSQL binaries once to
``.runtime/test-pg`` (gitignored), ``initdb``'s a data directory with trust
auth + ``--no-locale``, runs a server on an ephemeral port, and creates/drops
isolated databases per test. Linux/macOS: prefers a system PostgreSQL install
(``shutil.which`` then the Debian ``/usr/lib/postgresql/*/bin`` glob) and only
falls back to downloading the EDB linux-x64 binaries tarball (HEAD-verified).
No Docker, no third-party Python package (``embedded-postgres`` is not
available).

Usage (session fixture in conftest)::

    server = TestPostgres()
    server.start()
    try:
        yield server
    finally:
        server.stop()
"""

from __future__ import annotations

import platform
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen, urlretrieve

_PG_VERSION = "16.4"
_WINDOWS_URL_TEMPLATE = (
    "https://get.enterprisedb.com/postgresql/postgresql-{ver}-1-windows-x64-binaries.zip"
)
_LINUX_URL_TEMPLATE = (
    "https://get.enterprisedb.com/postgresql/postgresql-{ver}-1-linux-x64-binaries.tar.gz"
)
_DOWNLOAD_TIMEOUT_SECONDS = 900  # 338 MB zip
_HEAD_TIMEOUT_SECONDS = 30
_START_TIMEOUT_SECONDS = 60
_HOST = "127.0.0.1"
# Debian/Ubuntu postgres bin layout, e.g. /usr/lib/postgresql/16/bin.
_DEBIAN_PG_ROOT = Path("/usr/lib/postgresql")

# Implementations repo root (this file:
# implementations/services/product/backend_service/tests/integration/).
_REPO_ROOT = Path(__file__).resolve().parents[5]
_CACHE_DIR = _REPO_ROOT / ".runtime" / "test-pg"


def _free_port() -> int:
    """Reserve a free TCP port for the test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((_HOST, 0))
        return sock.getsockname()[1]


def _platform() -> str:
    """Return ``platform.system()`` — test seam for cross-platform tests."""
    return platform.system()


def _url_head_status(url: str) -> int:
    """Return the HTTP status of a HEAD request (test seam for network ops)."""
    with urlopen(Request(url, method="HEAD"), timeout=_HEAD_TIMEOUT_SECONDS) as resp:
        return resp.status


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
        self._external = False  # True when using a system postgres install

    # ── binaries ──────────────────────────────────────────────────────

    @staticmethod
    def _tool(name: str) -> str:
        """Binary name with ``.exe`` appended on Windows only."""
        return name + (".exe" if _platform() == "Windows" else "")

    @property
    def bin_dir(self) -> Path:
        if self._bin is None:
            raise RuntimeError("TestPostgres not started")
        return self._bin

    def _ensure_binaries(self) -> None:
        """Locate portable or system postgres binaries once (idempotent)."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        if _platform() == "Windows":
            self._ensure_windows_binaries()
        else:
            self._ensure_unix_binaries()

    def _ensure_windows_binaries(self) -> None:
        """Download + extract the portable EDB windows binaries (idempotent)."""
        zip_path = self.cache_dir / "pg16-binaries.zip"
        extracted = self.cache_dir / "pg16"
        pgsql = extracted / "pgsql"
        if pgsql.exists() and (pgsql / "bin" / "initdb.exe").exists():
            self._bin = pgsql / "bin"
            return
        if not zip_path.exists():
            url = _WINDOWS_URL_TEMPLATE.format(ver=self.version)
            print(f"[pg-harness] downloading {url} -> {zip_path}")
            urlretrieve(url, zip_path)
        print(f"[pg-harness] extracting {zip_path}")
        shutil.unpack_archive(str(zip_path), str(extracted))
        self._bin = pgsql / "bin"

    def _ensure_unix_binaries(self) -> None:
        """Prefer a system postgres install; fall back to EDB linux binaries."""
        system_dir = self._find_system_postgres()
        if system_dir is not None:
            self._bin = system_dir
            self._external = True
            print(f"[pg-harness] using system postgres from {system_dir}")
            return
        self._download_linux_binaries()

    def _find_system_postgres(self) -> Path | None:
        """Locate a system postgres bin dir via which, then the Debian glob."""
        bin_names = (self._tool("initdb"), self._tool("pg_ctl"), self._tool("postgres"))
        found = [shutil.which(name) for name in bin_names]
        if all(found):
            return Path(found[0]).parent
        for cand in sorted(_DEBIAN_PG_ROOT.glob("*/bin")):
            if all((cand / name).exists() for name in bin_names):
                return cand
        return None

    def _raise_install_postgres(self, reason: str) -> None:
        raise RuntimeError(
            "system postgres not found and EDB linux binaries unavailable "
            f"({reason}) — install postgresql (apt-get install postgresql) so "
            "initdb/pg_ctl/postgres are on PATH"
        )

    def _download_linux_binaries(self) -> None:
        """Best-effort EDB linux-x64 tarball download (HEAD-verified 200)."""
        url = _LINUX_URL_TEMPLATE.format(ver=self.version)
        try:
            status = _url_head_status(url)
        except HTTPError as exc:
            self._raise_install_postgres(f"HTTP {exc.code}")
        except URLError as exc:
            self._raise_install_postgres(str(exc.reason))
        if status != 200:
            self._raise_install_postgres(f"HTTP {status}")
        archive_path = self.cache_dir / "pg16-linux-binaries.tar.gz"
        extracted = self.cache_dir / "pg16-linux"
        if not archive_path.exists():
            print(f"[pg-harness] downloading {url} -> {archive_path}")
            urlretrieve(url, archive_path)
        print(f"[pg-harness] extracting {archive_path}")
        shutil.unpack_archive(str(archive_path), str(extracted))
        pgsql = extracted / "pgsql"
        self._bin = (pgsql if pgsql.exists() else extracted) / "bin"
        if not (self._bin / self._tool("initdb")).exists():
            self._raise_install_postgres("EDB tarball had no initdb binary")

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

        initdb = self.bin_dir / self._tool("initdb")
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

        pg_ctl = self.bin_dir / self._tool("pg_ctl")
        # DEVNULL (not capture_output): pg_ctl spawns the postmaster which
        # inherits pg_ctl's stdio; with a pipe, communicate() waits forever for
        # EOF that never comes while the server runs. Redirect to DEVNULL so
        # the server's stdio is the null device and subprocess.run returns
        # when pg_ctl (the direct child) exits.
        # unix_socket_directories=/tmp: on Linux CI (GH runner) the system
        # postgres default `/var/run/postgresql` may be missing/not writable
        # for the runner user, which makes the postmaster FATAL and pg_ctl
        # exit non-zero. /tmp is always writable and keeps the unix socket.
        # unix_socket_directories=/tmp only on Unix: on Linux CI the system
        # postgres default `/var/run/postgresql` may be missing/not writable
        # for the runner user, making the postmaster FATAL. Windows has no
        # meaningful unix-socket path and ignores the setting at best.
        unix_extra = " -c unix_socket_directories=/tmp" if _platform() != "Windows" else ""
        start_args = [
            str(pg_ctl),
            "-D",
            str(self._data_dir),
            "-l",
            str(self._log_path),
            "-o",
            f"-p {self.port} -h {self.host} -c listen_addresses={self.host}{unix_extra}",
            "start",
        ]
        try:
            subprocess.run(
                start_args,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError as exc:
            log_tail = ""
            if self._log_path and self._log_path.exists():
                log_tail = self._log_path.read_text(errors="ignore")[-2000:]
            raise RuntimeError(
                f"pg_ctl start failed (exit {exc.returncode}); "
                f"data dir: {self._data_dir}\n{log_tail}"
            ) from exc

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
        pg_ctl = self.bin_dir / self._tool("pg_ctl")
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
