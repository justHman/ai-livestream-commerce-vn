"""Download model weights from HuggingFace and upload to S3 for ECS GPU tasks.

Models (per docs/scope-engine-and-models.md):
  LLM:  cyankiwi/Qwen3.5-4B-AWQ-4bit      -> s3://.../weights/llm/
  TTS:  pnnbao-ump/VieNeu-TTS-v2           -> s3://.../weights/tts/vieneu/
  TTS:  neuphonic/neucodec                 -> s3://.../weights/tts/neucodec/

Usage:
  python scripts/upload_weights_s3.py --bucket ai-livestream-dev-assets-191918535424 --region ap-northeast-2
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


def upload_s3(local: Path, s3_uri: str, region: str) -> None:
    run(["aws", "s3", "sync", str(local), s3_uri, "--region", region, "--only-show-errors"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--region", default="ap-northeast-2")
    ap.add_argument("--cache", default=os.path.expanduser("~/.cache/ai-live-weights"))
    args = ap.parse_args()

    cache = Path(args.cache)
    base = f"s3://{args.bucket}/weights"

    # LLM: Qwen3.5-4B-AWQ-4bit (safetensors + config only, no gguf/onnx).
    llm_local = cache / "llm"
    if not (llm_local / "config.json").exists():
        download_hf("cyankiwi/Qwen3.5-4B-AWQ-4bit", llm_local,
                    patterns=["*.json", "*.safetensors", "*.model", "tokenizer*", "*.txt"])
    upload_s3(llm_local, f"{base}/llm/", args.region)

    # TTS: VieNeu-TTS-v2 (full checkpoint + voices.json).
    tts_local = cache / "tts" / "vieneu"
    if not (tts_local / "config.json").exists():
        download_hf("pnnbao-ump/VieNeu-TTS-v2", tts_local,
                    patterns=["*.json", "*.safetensors", "*.model", "tokenizer*",
                              "voices.json", "*.txt", "*.py", "*.wav"])
    upload_s3(tts_local, f"{base}/tts/vieneu/", args.region)

    # TTS: neucodec (codec decoder weights).
    nc_local = cache / "tts" / "neucodec"
    if not any(nc_local.glob("*.safetensors")) and not any(nc_local.glob("*.bin")):
        download_hf("neuphonic/neucodec", nc_local,
                    patterns=["*.json", "*.safetensors", "*.bin", "*.py", "*.txt"])
    upload_s3(nc_local, f"{base}/tts/neucodec/", args.region)

    print(f"\nWeights uploaded to {base}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())