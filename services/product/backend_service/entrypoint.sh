#!/usr/bin/env bash
# Backend has no model weights. Pass through to CMD (uvicorn).
set -euo pipefail
exec "$@"
