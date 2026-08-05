#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONPATH="/app/src/tts/..:${PYTHONPATH:-}"
if [[ $# -eq 0 ]]; then
  set -- sh -c 'exec vllm serve "${MODEL_ID}" --omni --tokenizer "${MODEL_ID}" --config-format hf --host 0.0.0.0 --port "${PORT}" --dtype half --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" --enforce-eager'
fi
exec "$(dirname "$0")/../entrypoint.sh" "$@"
