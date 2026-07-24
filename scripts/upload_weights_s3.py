"""Download model weights and upload to S3 for ECS GPU tasks.

Models (per docs/scope-engine-and-models.md):
  LLM:  cyankiwi/Qwen3.5-4B-AWQ-4bit      -> s3://.../weights/llm/        (HF, public)
  TTS:  VieNeu-TTS-v2                      -> s3://.../weights/tts/vieneu/ (LOCAL .git, NOT public on HF)
  TTS:  neuphonic/neucodec                 -> s3://.../weights/tts/neucodec/ (HF)

VieNeu-TTS-v2 is NOT public on HuggingFace. Its source is a local `.git` repo
(plus the registered adapter in the vllm-omni fork feat/vieneu-tts-v0.22).
Pass --vieneu-local-dir <path> to seed VieNeu from the local checkout instead
of attempting an HF pull that will fail. Qwen3.5-4B-AWQ and neucodec stay on HF
(the offline seeding source).

Usage:
  python scripts/upload_weights_s3.py --bucket ai-livestream-dev-assets-191918535424 --region ap-northeast-2 \
      --vieneu-local-dir /path/to/VieNeu-TTS-v2

Run OFFLINE (before any billable stage apply). NEVER run HF cold pulls or S3
syncs inside a billable stage window — S3 is the runtime source via
services/scripts/fetch_weights.sh.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], cwd: str | None = None) -> None:
    print(f"$ {' '.join(cmd)}")
    subprocess.run(cmd, cwd=cwd, check=True)


def download_hf(repo: str, dest: Path, patterns: list[str] | None = None) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download
    kwargs = {"repo_id": repo, "local_dir": str(dest)}
    if patterns:
        kwargs["allow_patterns"] = patterns
    print(f"Downloading {repo} -> {dest}")
    snapshot_download(**kwargs)
    print(f"  done: {dest}")


def stage_local(src: Path, dest: Path, name: str) -> None:
    """Copy a local directory (e.g. VieNeu .git checkout) into the cache for S3 sync."""
    if not src.exists():
        raise FileNotFoundError(f"--{name} {src} does not exist")
    dest.mkdir(parents=True, exist_ok=True)
    run(["robocopy", str(src), str(dest), "/MIR", "/NFL", "/NDL", "/NJH", "/NJS"])
    print(f"  staged {src} -> {dest}")


def upload_s3(local: Path, s3_uri: str, region: str) -> None:
    run(["aws", "s3", "sync", str(local), s3_uri, "--region", region, "--only-show-errors"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/ai-live-weights"))
    ap.add_argument("--vieneu-local-dir", default="",
                    help="Local .git checkout of VieNeu-TTS-v2 (NOT public on HF). Required to seed vieneu/.")
    ap.add_argument("--skip-llm", action="store_true", help="Skip Qwen3.5-4B-AWQ HF pull + upload")
    ap.add_argument("--skip-neucodec", action="store_true", help="Skip neucodec HF pull + upload")
    args = ap.parse_args()

    cache = Path(args.cache)
    base = f"s3://{args.bucket}/weights"

    # LLM: Qwen3.5-4B-AWQ-4bit (safetensors + config only, no gguf/onnx).
    if not args.skip_llm:
        llm_local = cache / "llm"
        if not (llm_local / "config.json").exists():
            download_hf("cyankiwi/Qwen3.5-4B-AWQ-4bit", llm_local,
                        patterns=["*.json", "*.safetensors", "*.model", "tokenizer*", "*.txt"])
        upload_s3(llm_local, f"{base}/llm/", args.region)

    # TTS: VieNeu-TTS-v2 — local .git source (NOT public on HF).
    tts_local = cache / "tts" / "vieneu"
    if args.vieneu_local_dir:
        stage_local(Path(args.vieneu_local_dir).resolve(), tts_local, "vieneu-local-dir")
    elif not (tts_local / "config.json").exists():
        raise SystemExit(
            "VieNeu-TTS-v2 is NOT public on HF. Pass --vieneu-local-dir <path to local .git checkout>."
        )
    upload_s3(tts_local, f"{base}/tts/vieneu/", args.region)

    # TTS: neucodec (codec decoder weights).
    if not args.skip_neucodec:
        nc_local = cache / "tts" / "neucodec"
        if not any(nc_local.glob("*.safetensors")) and not any(nc_local.glob("*.bin")):
            download_hf("neuphonic/neucodec", nc_local,
                        patterns=["*.json", "*.safetensors", "*.bin", "*.py", "*.txt"])
        upload_s3(nc_local, f"{base}/tts/neucodec/", args.region)

    print(f"\nWeights uploaded to {base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())