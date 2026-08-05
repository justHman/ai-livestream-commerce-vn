#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONPATH="/app/services/product/avatar_service/src:${PYTHONPATH:-}"
exec uvicorn health_app:app --host 0.0.0.0 --port "${PORT:-8080}"
