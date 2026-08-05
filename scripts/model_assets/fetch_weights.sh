#!/usr/bin/env bash
# Sync model weights from S3 into a staging dir, validate, then atomically publish.
# vLLM must NEVER observe a partially-synced model dir (root cause of
# "Invalid repository ID" / "LocalEntryNotFoundError" — vLLM 0.22 supports
# --model <local-dir> via Path.exists(), but the path must exist + contain
# config.json at resolve time).
#
# Env:
#   WEIGHTS_S3_URI     required, e.g. s3://bucket/weights/llm/
#   WEIGHTS_LOCAL_DIR  optional, default /models  (parent of model dir)
#   MODEL_SUBDIR       optional, default qwen3-4b-awq (final path = WEIGHTS_LOCAL_DIR/MODEL_SUBDIR)
#   AWS_DEFAULT_REGION optional (task role / instance profile supplies credentials)
set -Eeuo pipefail

: "${WEIGHTS_S3_URI:?WEIGHTS_S3_URI is required}"

DEST_ROOT="${WEIGHTS_LOCAL_DIR:-/models}"
MODEL_SUBDIR="${MODEL_SUBDIR:-qwen3-4b-awq}"
MODEL_DIR="${DEST_ROOT}/${MODEL_SUBDIR}"
STAGING="${DEST_ROOT}/.${MODEL_SUBDIR}.partial.$$"

rm -rf "${STAGING}"
mkdir -p "${STAGING}"

echo "[fetch_weights] sync ${WEIGHTS_S3_URI} -> ${STAGING} (staging)"
if ! command -v aws >/dev/null 2>&1; then
  echo "[fetch_weights] ERROR: aws CLI not found in image" >&2
  exit 1
fi
aws s3 sync "${WEIGHTS_S3_URI}" "${STAGING}" --only-show-errors

# Validate required files before publishing (fail loud, no partial dir).
python - <<PY
import glob, json, os
from pathlib import Path
model = Path(os.environ["STAGING"])
required = [model / "config.json"]
for p in required:
    if not p.is_file() or p.stat().st_size == 0:
        raise RuntimeError(f"Missing or empty required file: {p}")
with open(model / "config.json", encoding="utf-8") as f:
    cfg = json.load(f)
archs = cfg.get("architectures") or []
if "Qwen3ForCausalLM" not in archs:
    raise RuntimeError(f"Unexpected architectures: {archs!r}")
weights = glob.glob(str(model / "*.safetensors"))
if not weights:
    raise RuntimeError("No safetensors weights found")
for w in weights:
    if Path(w).stat().st_size == 0:
        raise RuntimeError(f"Empty weight file: {w}")
print(f"[fetch_weights] validated: {len(weights)} weights, arch={archs}")
PY

# Atomic publish: rename staging -> final (same filesystem, atomic).
rm -rf "${MODEL_DIR}"
mv "${STAGING}" "${MODEL_DIR}"
touch "${MODEL_DIR}/.ready"

echo "[fetch_weights] published ${MODEL_DIR} (.ready)"
