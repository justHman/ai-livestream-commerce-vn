#!/usr/bin/env bash
# Local/container start helper for the TTS service (provider-neutral).
# Defaults to uvicorn on tts.main:app; extra args pass through.
set -Eeuo pipefail

export PYTHONPATH="/app/services/product/tts_service/src:${PYTHONPATH:-}"
if [[ $# -eq 0 ]]; then
  set -- uvicorn tts.main:app --host 0.0.0.0 --port "${PORT:-8002}"
fi
exec "$(dirname "$0")/../entrypoint.sh" "$@"
