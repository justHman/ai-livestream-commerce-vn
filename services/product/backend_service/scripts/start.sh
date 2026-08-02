#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONPATH="/app/services/product/backend_service/src:/app/services/product/llm_service/src:/app/services/product/tts_service/src:/app/services/product/avatar_service/src:/app:${PYTHONPATH:-}"
exec uvicorn backend.main:app --host 0.0.0.0 --port "${PORT:-8800}"
