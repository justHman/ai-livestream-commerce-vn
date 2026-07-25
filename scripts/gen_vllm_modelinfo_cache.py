"""Pre-generate vLLM _ModelInfo cache JSON (PR #23558 pattern) to skip
`_run_in_subprocess` model inspect that hangs on L4 + receives SIGINT -> crash.

vLLM 0.22 `model_executor/models/registry.py` `_LazyRegisteredModel.inspect_model_cls`
calls `_run_in_subprocess` to import the model class and build `_ModelInfo`.
Subprocess import torch+CUDA is slow (~10s) on L4 and receives SIGINT from ECS
during long init -> `KeyboardInterrupt: terminated` -> container exit 1.

PR #23558 (merged after v0.22) saves `_ModelInfo` to
`$VLLM_CACHE_ROOT/modelinfos/{module}-{class}.json` so subsequent loads read
cache and skip subprocess. vLLM 0.22 does NOT have this. We pre-generate the
cache file so vLLM 0.22 reads it (after patching registry.py to check cache
first — see Dockerfile patch step) and skips the subprocess.

Cache schema (PR #23558):
  {
    "hash": md5(model_file_bytes),
    "modelinfo": {< _ModelInfo dataclass fields asdict >}
  }

File name: `{module_name}-{class_name}.json` with dots -> dashes.
For Qwen3ForCausalLM: module `vllm.model_executor.models.qwen3`,
class `Qwen3ForCausalLM` -> file
`vllm-model_executor-models-qwen3-Qwen3ForCausalLM.json`.

Usage:
  python scripts/gen_vllm_modelinfo_cache.py --out /tmp/modelinfos
  # then bake /tmp/modelinfos into image at $VLLM_CACHE_ROOT/modelinfos/

Verify module_hash by fetching qwen3.py from the vLLM 0.22.0 tag:
  curl -sL https://raw.githubusercontent.com/vllm-project/vllm/v0.22.0/vllm/model_executor/models/qwen3.py | md5sum
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

# _ModelInfo fields for Qwen3ForCausalLM (text generation, standard attention,
# no multimodal/pooling/transcription). Matches vLLM 0.22 _ModelInfo dataclass.
QWEN3_MODELINFO = {
    "architecture": "Qwen3ForCausalLM",
    "is_text_generation_model": True,
    "is_pooling_model": False,
    "attn_type": "default",
    "default_seq_pooling_type": "last",
    "default_tok_pooling_type": "mean",
    "score_type": "unconditional",
    "supports_multimodal": False,
    "supports_multimodal_raw_input_only": False,
    "requires_raw_input_tokens": False,
    "supports_multimodal_encoder_tp_data": False,
    "supports_pp": True,
    "has_inner_state": False,
    "is_attention_free": False,
    "is_hybrid": False,
    "has_noops": False,
    "supports_mamba_prefix_caching": False,
    "supports_transcription": False,
    "supports_transcription_only": False,
}

# md5 of vllm/model_executor/models/qwen3.py at v0.22.0 tag.
# Re-verify if base image changes: curl raw qwen3.py @ v0.22.0 | md5sum.
QWEN3_MODULE_HASH = "110fffb133f8363ff09b733c36bd380f"


def write_cache(out_dir: Path, module_name: str, class_name: str,
                modelinfo: dict, module_hash: str) -> Path:
    fname = f"{module_name}-{class_name}".replace(".", "-") + ".json"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / fname
    payload = {"hash": module_hash, "modelinfo": modelinfo}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="output modelinfos/ dir")
    args = ap.parse_args()
    out = Path(args.out)
    p = write_cache(out, "vllm.model_executor.models.qwen3",
                    "Qwen3ForCausalLM", QWEN3_MODELINFO, QWEN3_MODULE_HASH)
    print(f"wrote {p}")
    print(f"  hash={QWEN3_MODULE_HASH}")
    print(f"  bake into image at $VLLM_CACHE_ROOT/modelinfos/ (default ~/.cache/vllm/modelinfos)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
