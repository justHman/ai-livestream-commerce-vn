#!/usr/bin/env bash
# Avatar GPU entrypoint: optional S3 weight sync, then exec CMD.
set -euo pipefail

export WEIGHTS_LOCAL_DIR="${WEIGHTS_LOCAL_DIR:-/models}"
export PORT="${PORT:-8080}"

if [[ -n "${WEIGHTS_S3_URI:-}" ]]; then
  /usr/local/bin/fetch_weights.sh
else
  echo "[avatar-entrypoint] WEIGHTS_S3_URI unset — skeleton health server only"
fi

exec "$@"
