#!/usr/bin/env sh
# LiveKit SFU entrypoint. No model weights.
set -eu

CONFIG="${LIVEKIT_CONFIG:-/etc/livekit.yaml}"

if [ -n "${LIVEKIT_KEYS:-}" ]; then
  # LIVEKIT_KEYS format: "APIxxx: secret" (official env support may also apply).
  echo "[livekit] LIVEKIT_KEYS provided via env"
fi

echo "[livekit] starting with config ${CONFIG}"
# Official image entry is livekit-server; pass remaining args through.
exec livekit-server "$@"
