#!/usr/bin/env bash
# TTS entrypoint: optional S3 weight sync, then exec the uvicorn CMD.
# The VieNeu v3 Turbo provider loads its model from HF or the local cache;
# WEIGHTS_S3_URI (when set) syncs pre-seeded weights first, mirroring the
# air-gapped pattern of the LLM service. Never bake weights into the image.
set -Eeuo pipefail

export WEIGHTS_LOCAL_DIR="${WEIGHTS_LOCAL_DIR:-/models}"
export MODEL_SUBDIR="${MODEL_SUBDIR:-vieneu}"
export MODEL_ID="${WEIGHTS_LOCAL_DIR}/${MODEL_SUBDIR}"
export PORT="${PORT:-8002}"
export HF_HOME="${HF_HOME:-/var/cache/huggingface}"
export HF_HUB_CACHE="${HF_HUB_CACHE:-${HF_HOME}/hub}"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-0}"
export HF_HUB_DISABLE_TELEMETRY="${HF_HUB_DISABLE_TELEMETRY:-1}"
export DO_NOT_TRACK="${DO_NOT_TRACK:-1}"

if [[ -n "${WEIGHTS_S3_URI:-}" ]]; then
  /usr/local/bin/fetch_weights.sh
  i=0
  until [[ -f "${MODEL_ID}/.ready" ]]; do
    i=$((i+1)); [[ $i -gt 60 ]] && { echo "[tts-entrypoint] .ready timeout" >&2; exit 1; }
    sleep 1
  done
  echo "[tts-entrypoint] weights ready at ${MODEL_ID}"
fi

echo "[tts-entrypoint] exec uvicorn tts.main:app (provider=${TTS_PROVIDER:-vieneu_v3})"
exec "$@"
