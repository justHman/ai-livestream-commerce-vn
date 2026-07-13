#!/usr/bin/env bash
# LMCache entrypoint. No weight sync (stateful RAM cache only).
set -euo pipefail

export PORT_METRICS="${PORT_METRICS:-8080}"
export PORT_ZMQ="${PORT_ZMQ:-5555}"

if command -v lmcache-server >/dev/null 2>&1; then
  echo "[lmcache] starting lmcache-server zmq=:${PORT_ZMQ} metrics=:${PORT_METRICS}"
  exec lmcache-server --port "${PORT_ZMQ}" --metrics-port "${PORT_METRICS}" "$@"
fi

if command -v lmcache >/dev/null 2>&1; then
  echo "[lmcache] starting lmcache CLI"
  exec lmcache "$@"
fi

echo "[lmcache] binary missing — skeleton metrics on :${PORT_METRICS}"
exec "$@"
