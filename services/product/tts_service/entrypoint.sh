#!/usr/bin/env bash
# TTS GPU entrypoint: atomic S3 weight sync → validate → exec vllm-omni serve.
# Same root cause fix as LLM: vLLM 0.22 supports --model <local-dir> via
# Path.exists(); path must exist + contain config.json at resolve time.
# fetch_weights.sh does staging → validate → atomic publish → .ready.
# We exec vllm-omni only after .ready exists.
set -Eeuo pipefail

export WEIGHTS_LOCAL_DIR="${WEIGHTS_LOCAL_DIR:-/models}"
export MODEL_SUBDIR="${MODEL_SUBDIR:-vieneu}"
export MODEL_ID="${WEIGHTS_LOCAL_DIR}/${MODEL_SUBDIR}"
export PORT="${PORT:-8002}"
export GPU_MEMORY_UTILIZATION="${GPU_MEMORY_UTILIZATION:-0.35}"
export PYTHONPATH="/opt/vllm-omni:${PYTHONPATH:-}"

# Separate HF cache from model dir.
export HF_HOME="${HF_HOME:-/var/cache/huggingface}"
export HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TRANSFORMERS_OFFLINE="${TRANSFORMERS_OFFLINE:-1}"
export HF_HUB_DISABLE_TELEMETRY="${HF_HUB_DISABLE_TELEMETRY:-1}"
export VLLM_NO_USAGE_STATS="${VLLM_NO_USAGE_STATS:-1}"
export DO_NOT_TRACK="${DO_NOT_TRACK:-1}"

if [[ -n "${WEIGHTS_S3_URI:-}" ]]; then
  /usr/local/bin/fetch_weights.sh
  i=0
  until [[ -f "${MODEL_ID}/.ready" ]]; do
    i=$((i+1)); [[ $i -gt 60 ]] && { echo "[tts-entrypoint] .ready timeout" >&2; exit 1; }
    sleep 1
  done
else
  echo "[tts-entrypoint] WEIGHTS_S3_URI unset — using ${MODEL_ID} / HF cache"
fi

echo "[tts-entrypoint] exec vllm serve ${MODEL_ID} --omni"
exec "$@"
