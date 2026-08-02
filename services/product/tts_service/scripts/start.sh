#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONPATH="/app/src/tts/..:${PYTHONPATH:-}"
exec "$(dirname "$0")/../entrypoint.sh" "$@"
