#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONPATH="/app/src/llm/..:${PYTHONPATH:-}"
if [[ $# -eq 0 ]]; then
  set -- sh -c 'exec vllm serve "${MODEL_ID}" --tokenizer "${MODEL_ID}" --config-format hf --model-impl auto --served-model-name qwen3-4b-awq --host 0.0.0.0 --port "${PORT}" --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}" --enforce-eager --quantization awq_marlin $( [ "${ENABLE_PREFIX_CACHING}" = "1" ] && echo --enable-prefix-caching )'
fi
exec "$(dirname "$0")/../entrypoint.sh" "$@"
