"""Platform-awareness unit tests for the embedded-PostgreSQL harness (C10).

No server boot, no network: mock ``_platform()`` / ``shutil.which`` / the
Debian glob root / the EDB HEAD check so the binary-location decision logic is
exercised on any host OS.
"""

from __future__ import annotations

from pathlib import Path
from urllib.error import HTTPError

import pytest

from . import pg_harness


def _server(tmp_path: Path) -> pg_harness.TestPostgres:
    return pg_harness.TestPostgres(cache_dir=tmp_path / "cache")


def test_tool_appends_exe_only_on_windows(monkeypatch) -> None:
    monkeypatch.setattr(pg_harness, "_platform", lambda: "Windows")
    assert pg_harness.TestPostgres._tool("initdb") == "initdb.exe"
    monkeypatch.setattr(pg_harness, "_platform", lambda: "Linux")
    assert pg_harness.TestPostgres._tool("initdb") == "initdb"
    assert pg_harness.TestPostgres._tool("pg_ctl") == "pg_ctl"
    monkeypatch.setattr(pg_harness, "_platform", lambda: "Darwin")
    assert pg_harness.TestPostgres._tool("postgres") == "postgres"


def test_linux_system_postgres_via_which_no_download(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pg_harness, "_platform", lambda: "Linux")
    monkeypatch.setattr(
        pg_harness.shutil, "which", lambda name: f"/usr/lib/postgresql/16/bin/{name}"
    )
    server = _server(tmp_path)
    server._ensure_binaries()  # noqa: SLF001 - test exercises the seam directly
    assert server._bin == Path("/usr/lib/postgresql/16/bin")
    assert server._external is True


def test_linux_system_postgres_via_debian_glob(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pg_harness, "_platform", lambda: "Linux")
    monkeypatch.setattr(pg_harness.shutil, "which", lambda name: None)
    fake_root = tmp_path / "usr" / "lib" / "postgresql"
    bin_dir = fake_root / "16" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("initdb", "pg_ctl", "postgres"):
        (bin_dir / name).write_text("", encoding="utf-8")
    monkeypatch.setattr(pg_harness, "_DEBIAN_PG_ROOT", fake_root)

    server = _server(tmp_path)
    server._ensure_binaries()  # noqa: SLF001
    assert server._bin == bin_dir
    assert server._external is True


def test_linux_neither_system_nor_edb_raises_install_hint(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pg_harness, "_platform", lambda: "Linux")
    monkeypatch.setattr(pg_harness.shutil, "which", lambda name: None)
    monkeypatch.setattr(pg_harness, "_DEBIAN_PG_ROOT", tmp_path / "no-postgres")

    def fake_head(u: str) -> int:  # pragma: no cover - the 403 path never returns
        raise HTTPError(u, 403, "Forbidden", None, None)

    monkeypatch.setattr(pg_harness, "_url_head_status", fake_head)
    server = _server(tmp_path)
    with pytest.raises(RuntimeError, match="install postgresql"):
        server._ensure_binaries()  # noqa: SLF001


def test_linux_edb_head_non_200_raises_install_hint(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(pg_harness, "_platform", lambda: "Linux")
    monkeypatch.setattr(pg_harness.shutil, "which", lambda name: None)
    monkeypatch.setattr(pg_harness, "_DEBIAN_PG_ROOT", tmp_path / "no-postgres")
    monkeypatch.setattr(pg_harness, "_url_head_status", lambda u: 404)
    server = _server(tmp_path)
    with pytest.raises(RuntimeError, match="install postgresql"):
        server._ensure_binaries()  # noqa: SLF001
