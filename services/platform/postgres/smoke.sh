#!/usr/bin/env sh
set -eu

: "${DATABASE_URL:?DATABASE_URL is required}"
python - <<'PY'
import asyncio
import os

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(os.environ["DATABASE_URL"])
    await connection.close()
    print("postgres ok")


asyncio.run(main())
PY
