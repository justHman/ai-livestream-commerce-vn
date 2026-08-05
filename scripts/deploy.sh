#!/usr/bin/env bash
# deploy.sh — dispatch dev/staging deployment via gh CLI (OpenSpec 4.3).
# Usage: scripts/deploy.sh <dev|staging> <full-40-hex-sha> <services> [watch]
#   scripts/deploy.sh dev abcdef... backend_service,tts_service
set -euo pipefail

env_name="${1:-}"
sha="${2:-}"
services="${3:-}"
watch="${4:-}"

if [[ "$env_name" != "dev" && "$env_name" != "staging" ]]; then
  echo "Usage: scripts/deploy.sh <dev|staging> <sha> <services> [watch]" >&2
  echo "Environment must be 'dev' or 'staging'." >&2
  exit 1
fi

if [[ ! "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "SHA must be a full 40-hex commit SHA." >&2
  exit 1
fi

if [[ -z "$services" ]]; then
  echo "Services list must not be empty (e.g. backend_service,tts_service)." >&2
  exit 1
fi

gh auth status >/dev/null 2>&1 || {
  echo "gh is not authenticated. Run: gh auth login" >&2
  exit 1
}

if [[ "$env_name" == "dev" ]]; then
  workflow="deploy-dev.yml"
  ref="develop"
else
  workflow="deploy-staging.yml"
  ref="main"
fi

gh workflow run "$workflow" --ref "$ref" -f "commit_sha=$sha" -f "services=$services"

run_url=$(gh run list --workflow "$workflow" -L 1 --json url --jq '.[0].url')
echo "Dispatched $workflow (ref=$ref) for $sha"
echo "Run: $run_url"

if [[ "$watch" == "watch" ]]; then
  gh run watch --exit-status
fi
