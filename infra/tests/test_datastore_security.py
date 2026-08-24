"""C.3 managed data-store security — static infra contract checks (no terraform).

Static text assertions over the data-store security settings only: managed
Redis becomes an ``aws_elasticache_replication_group`` with transit+at-rest
encryption and an optional AUTH token (R7.1); the Postgres parameter group
forces ``rds.force_ssl`` server-side (R7.4); and the module/environment wiring
exposes a sensitive ``rediss://`` URI instead of a plaintext host:port string.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB_MAIN = ROOT / "infra/modules/database/main.tf"
DB_OUTPUTS = ROOT / "infra/modules/database/outputs.tf"
# Only deployment envs that create the database module — excludes the account
# bootstrap in environments/global, which has no RDS/ElastiCache to secure.
ENVIRONMENTS = sorted(
    p
    for p in (ROOT / "infra/environments").glob("*/main.tf")
    if 'module "database"' in p.read_text(encoding="utf-8")
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _compact(source: str) -> str:
    """Collapse whitespace runs to single spaces so terraform-fmt aligned
    ``key = value`` pairs match the same literal tokens."""
    return re.sub(r"\s+", " ", source)


def test_redis_uses_replication_group_with_tls_and_auth() -> None:
    source = _read(DB_MAIN)
    assert 'resource "aws_elasticache_replication_group"' in source
    assert 'resource "aws_elasticache_cluster"' not in source
    assert "transit_encryption_enabled = true" in source
    assert "at_rest_encryption_enabled = true" in source
    assert "auth_token = var.redis_auth_token" in _compact(source)


def test_postgres_parameter_group_forces_ssl() -> None:
    source = _read(DB_MAIN)
    assert "rds.force_ssl" in source
    assert 'value = "1"' in _compact(source)


def test_redis_uri_output_is_sensitive_rediss() -> None:
    source = _read(DB_OUTPUTS)
    assert "rediss://" in source
    assert "sensitive   = true" in source


def test_environments_use_module_redis_uri() -> None:
    for main in ENVIRONMENTS:
        source = _read(main)
        assert "module.database.redis_uri" in source
        assert '"redis://${' not in source
