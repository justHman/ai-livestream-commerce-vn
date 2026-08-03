#!/usr/bin/env bash
# LMCache standalone MP server entrypoint.
# Real upstream runtime only. No synthetic metrics, no fallback process, no
# best-effort install. Missing binary/package/config FAILS here.
set -euo pipefail

export PORT_HTTP="${PORT_HTTP:-8080}"
export PORT_ZMQ="${PORT_ZMQ:-5555}"

if ! command -v lmcache >/dev/null 2>&1; then
  echo "[lmcache] FATAL: lmcache CLI not installed" >&2
  exit 1
fi

echo "[lmcache] starting lmcache server zmq=:${PORT_ZMQ} http=:${PORT_HTTP}"
exec lmcache server \
  --host 0.0.0.0 \
  --port "${PORT_ZMQ}" \
  --http-host 0.0.0.0 \
  --http-port "${PORT_HTTP}" \
  "$@"