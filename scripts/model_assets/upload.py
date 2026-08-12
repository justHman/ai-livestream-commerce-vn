"""Download model weights and upload to S3 for ECS GPU tasks.

Models:
  LLM:  Qwen/Qwen3-4B-AWQ                 -> s3://.../weights/llm/        (HF, public, vLLM 0.22 native)
  TTS:  pnnbao-ump/VieNeu-TTS-v3-Turbo    -> s3://.../weights/tts/vieneu/ (HF, public)
  TTS:  neuphonic/neucodec                -> s3://.../weights/tts/neucodec/ (HF, public)

LLM switched from cyankiwi/Qwen3.5-4B-AWQ-4bit (custom Qwen3_5ForConditionalGeneration
fork unsupported by vLLM 0.22 model registry) to Qwen/Qwen3-4B-AWQ (Qwen3ForCausalLM,
vLLM 0.22 native support). All three repos are public on HF (verified 2026-07-24).

Usage:
  python scripts/upload_weights_s3.py --bucket ai-livestream-dev-assets-191918535424 --region ap-northeast-2

Run OFFLINE (before any billable stage apply). NEVER run HF cold pulls or S3
syncs inside a billable stage window — S3 is the runtime source via
scripts/model_assets/fetch_weights.sh.
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
                    help="Local .git checkout of the VieNeu-TTS-v3-Turbo weights (NOT public on HF). Required to seed vieneu/.")
    ap.add_argument("--skip-llm", action="store_true", help="Skip Qwen3.5-4B-AWQ HF pull + upload")
    ap.add_argument("--skip-neucodec", action="store_true", help="Skip neucodec HF pull + upload")
    args = ap.parse_args()

    cache = Path(args.cache)
    base = f"s3://{args.bucket}/weights"

    # LLM: Qwen/Qwen3-4B-AWQ (arch Qwen3ForCausalLM — vLLM 0.22 native support).
    # Replaces cyankiwi/Qwen3.5-4B-AWQ-4bit (custom Qwen3_5ForConditionalGeneration
    # fork that vLLM 0.22 model registry cannot inspect -> subprocess crash).
    if not args.skip_llm:
        llm_local = cache / "llm"
        has_weights = any(llm_local.glob("*.safetensors"))
        if not has_weights:
            download_hf("Qwen/Qwen3-4B-AWQ", llm_local,
                        patterns=["*.json", "*.safetensors", "*.model", "tokenizer*", "*.txt"])
        upload_s3(llm_local, f"{base}/llm/", args.region)

    # TTS: VieNeu-TTS-v3-Turbo — HF public (pnnbao-ump/VieNeu-TTS-v3-Turbo).
    # --vieneu-local-dir is an optional fallback for offline/airgapped.
    tts_local = cache / "tts" / "vieneu"
    if args.vieneu_local_dir:
        stage_local(Path(args.vieneu_local_dir).resolve(), tts_local, "vieneu-local-dir")
    elif not (tts_local / "config.json").exists():
        download_hf("pnnbao-ump/VieNeu-TTS-v3-Turbo", tts_local,
                    patterns=["*.json", "*.safetensors", "*.model", "tokenizer*",
                              "voices.json", "*.txt", "*.py", "*.wav"])
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