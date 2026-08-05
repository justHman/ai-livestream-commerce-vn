#!/usr/bin/env sh
# LiveKit SFU entrypoint. No model weights.
# Missing key/secret/config MUST fail startup — never start with empty keys.
set -eu

CONFIG="${LIVEKIT_CONFIG:-/etc/livekit.yaml}"
if [ ! -f "${CONFIG}" ]; then
  echo "[livekit] FATAL: config file missing at ${CONFIG}" >&2
  exit 1
fi

if [ -z "${LIVEKIT_API_KEY:-}" ] || [ -z "${LIVEKIT_API_SECRET:-}" ]; then
  echo "[livekit] FATAL: LIVEKIT_API_KEY and LIVEKIT_API_SECRET are required (LiveKit Cloud)" >&2
  exit 1
fi

export LIVEKIT_KEYS="${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}"

echo "[livekit] starting with config ${CONFIG}"
exec livekit-server --config "${CONFIG}" "$@"