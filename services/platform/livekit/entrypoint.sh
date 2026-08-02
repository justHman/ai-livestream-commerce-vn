#!/usr/bin/env sh
# LiveKit SFU entrypoint. No model weights.
# Builds LIVEKIT_KEYS ("APIkey: secret") from SSM-injected API_KEY + API_SECRET.
set -eu

CONFIG="${LIVEKIT_CONFIG:-/etc/livekit.yaml}"

if [ -n "${LIVEKIT_API_KEY:-}" ] && [ -n "${LIVEKIT_API_SECRET:-}" ]; then
  export LIVEKIT_KEYS="${LIVEKIT_API_KEY}: ${LIVEKIT_API_SECRET}"
  echo "[livekit] LIVEKIT_KEYS built from SSM API_KEY/API_SECRET"
else
  echo "[livekit] WARN: LIVEKIT_API_KEY/API_SECRET unset — keys empty"
fi

echo "[livekit] starting with config ${CONFIG}"
exec livekit-server --config "${CONFIG}" "$@"
