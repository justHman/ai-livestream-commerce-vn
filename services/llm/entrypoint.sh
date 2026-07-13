#!/usr/bin/env bash
# LLM GPU entrypoint: optional S3 weight sync, then exec CMD (vllm serve).
set -euo pipefail

export WEIGHTS_LOCAL_DIR="${WEIGHTS_LOCAL_DIR:-/models}"
export MODEL_ID="${MODEL_ID:-cyankiwi/Qwen3.5-4B-AWQ-4bit}"
export PORT="${PORT:-8001}"
export GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.6}"
export ENABLE_PREFIX_CACHING="${ENABLE_PREFIX_CACHING:-1}"

if [[ -n "${WEIGHTS_S3_URI:-}" ]]; then
  /usr/local/bin/fetch_weights.sh
else
  echo "[llm-entrypoint] WEIGHTS_S3_URI unset — using ${WEIGHTS_LOCAL_DIR} / HF cache"
fi

exec "$@"
