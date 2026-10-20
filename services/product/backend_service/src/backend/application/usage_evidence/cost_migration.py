"""One-shot: widen the usage_evidence_outbox event_type CHECK for ``ai.cost.reported`` (M7.3).

    DATABASE_URL=... python -m backend.application.usage_evidence.cost_migration

Run once before enabling USAGE_EVIDENCE_COST_ENABLED. Idempotent; never run at app startup.
"""

from __future__ import annotations

import asyncio
import os

from backend.application.db.postgres_store import PostgresRuntimeStore


async def main() -> int:
    store = PostgresRuntimeStore(os.environ.get("DATABASE_URL", ""))
    if not store.enabled:
        print("DATABASE_URL is not set")
        return 2
    await store.connect()
    try:
        changed = await store.migrate_usage_cost_event_type()
    finally:
        await store.close()
    print("widened event_type CHECK" if changed else "already widened; nothing to do")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
