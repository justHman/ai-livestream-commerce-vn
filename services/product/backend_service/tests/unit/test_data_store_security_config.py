"""C.3 managed data-store security — fail-loud production invariants (RED).

Production (app_env in {"prod", "production"}) must never boot against an
unencrypted data store: Redis URLs must use the TLS ``rediss://`` scheme and a
Postgres DSN must carry an explicit ``sslmode`` of at least ``require``. These
are the app-side half of the R7.1 (Redis auth+transit TLS) and R7.4 (RDS TLS as
an app contract) remediations. Local/dev configurations stay unrestricted.
"""

from __future__ import annotations

import pytest

from backend.config import AppConfig


def test_prod_redis_requires_tls_scheme() -> None:
    with pytest.raises(ValueError, match="rediss://"):
        AppConfig(app_env="prod", store_backend="redis", redis_url="redis://h:6379/0")

    ok = AppConfig(app_env="prod", store_backend="redis", redis_url="rediss://h:6379/0")
    assert ok.redis_url == "rediss://h:6379/0"


def test_prod_database_requires_sslmode() -> None:
    with pytest.raises(ValueError, match="sslmode"):
        AppConfig(app_env="prod", database_url="postgresql://u:p@h:5432/db")

    AppConfig(app_env="prod", database_url="postgresql://u:p@h:5432/db?sslmode=require")
    AppConfig(app_env="prod", database_url="postgresql://u:p@h:5432/db?sslmode=verify-full")


def test_prod_loopback_database_needs_no_sslmode() -> None:
    # A local/embedded Postgres on loopback is an explicit dev/test store
    # (C.3.3): the prod TLS gate exempts it so embedded-PG integration fixtures
    # keep working without TLS.
    AppConfig(app_env="prod", database_url="postgresql://u:p@127.0.0.1:5432/db")
    AppConfig(app_env="prod", database_url="postgresql://u:p@localhost:5432/db")


def test_dev_local_stores_unaffected() -> None:
    AppConfig(app_env="dev", store_backend="redis", redis_url="redis://localhost:6379/0")
    AppConfig(app_env="dev", database_url="postgresql://u:p@h:5432/db")
    AppConfig()
