"""Pre-deploy runtime schema migration entrypoint (task 1.71).

Runs additive raw-SQL migrations from ``backend/db/sql/runtime_schema.sql``
once, before the backend rollout. Exits non-zero when ``DATABASE_URL`` is
missing (the ECS task always provides it via SSM) or the migration fails,
so the deployment gate aborts instead of shipping an un-migrated revision.
"""

from __future__ import annotations

import asyncio
import os
import sys

from backend.application.db.postgres_store import PostgresRuntimeStore


async def _main() -> int:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("migrate: DATABASE_URL is not set; refusing to run", file=sys.stderr)
        return 1
    store = PostgresRuntimeStore(database_url)
    try:
        await store.connect()
        await store.apply_schema()
    except Exception as exc:  # noqa: BLE001 - fail loud, the gate must abort
        print(f"migrate: failed: {exc!r}", file=sys.stderr)
        return 1
    finally:
        try:
            await store.close()
        except Exception:
            pass
    print("migrate: runtime schema applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
