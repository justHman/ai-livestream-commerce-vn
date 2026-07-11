#!/usr/bin/env bash
# TTS GPU entrypoint: optional S3 weight sync, then exec CMD (vllm-omni serve).
set -euo pipefail

export WEIGHTS_LOCAL_DIR="${WEIGHTS_LOCAL_DIR:-/models}"
export MODEL_ID="${MODEL_ID:-pnnbao-ump/VieNeu-TTS-v2}"
export PORT="${PORT:-8002}"
export GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.25}"

if [[ -n "${WEIGHTS_S3_URI:-}" ]]; then
  /usr/local/bin/fetch_weights.sh
else
  echo "[tts-entrypoint] WEIGHTS_S3_URI unset — using ${WEIGHTS_LOCAL_DIR} / HF cache"
fi

exec "$@"
