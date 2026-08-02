#!/usr/bin/env sh
set -eu

REDIS_URL="${REDIS_URL:-redis://127.0.0.1:6379/0}"
export REDIS_URL
python -c 'import os; import redis; client = redis.from_url(os.environ["REDIS_URL"]); client.ping(); print("redis ok")'
