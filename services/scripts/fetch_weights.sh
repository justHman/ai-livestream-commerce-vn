#!/usr/bin/env bash
# Sync model weights from S3 into a local directory.
# Weights stay OUT of the Docker image (docs/aws-architecture.md §2.5).
#
# Env:
#   WEIGHTS_S3_URI     required, e.g. s3://ai-livestream-dev/weights/llm/
#   WEIGHTS_LOCAL_DIR  optional, default /models
#   AWS_DEFAULT_REGION optional (task role / instance profile supplies credentials)
set -euo pipefail

if [[ -z "${WEIGHTS_S3_URI:-}" ]]; then
  echo "[fetch_weights] WEIGHTS_S3_URI unset — skip sync" >&2
  exit 0
fi

DEST="${WEIGHTS_LOCAL_DIR:-/models}"
mkdir -p "${DEST}"

echo "[fetch_weights] sync ${WEIGHTS_S3_URI} -> ${DEST}"
if ! command -v aws >/dev/null 2>&1; then
  echo "[fetch_weights] ERROR: aws CLI not found in image" >&2
  exit 1
fi

aws s3 sync "${WEIGHTS_S3_URI}" "${DEST}" --only-show-errors
echo "[fetch_weights] done"
