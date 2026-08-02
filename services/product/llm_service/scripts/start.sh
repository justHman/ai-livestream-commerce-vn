#!/usr/bin/env bash
set -Eeuo pipefail

export PYTHONPATH="/app/src/llm/..:${PYTHONPATH:-}"
exec "$(dirname "$0")/../entrypoint.sh" "$@"
