# vLLM 0.22 local-dir parse regression — research prompt

## Context (agent chưa biết gì về project này)

Project: AI live-commerce host backend (FastAPI control plane + LiveKit media + vLLM LLM + vllm-omni TTS, deploy trên AWS ECS EC2 g6.xlarge Spot L4 24GB, ap-northeast-2). Self-host LLM+vLLM phục vụ Stage 2 (LiveAvatar cloud + real engines) và Stage 3 (self-host avatar). Weights seed S3 runtime (KHÔNG HF cold pull trong billable window vì HF throttle VN ~37KB/s).

Stack: vLLM 0.22.0 (base image `vllm/vllm-openai:v0.22.0`), model `Qwen/Qwen3-4B-AWQ` (arch `Qwen3ForCausalLM`, AWQ-INT4 compressed-tensors, 2.68GB). Weights synced từ S3 `s3://bucket/weights/llm/*` vào `/models/` (flat: `/models/config.json`, `/models/model-00001-of-00001.safetensors`, tokenizer files). Container chạy với env `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `HF_HUB_DISABLE_TELEMETRY=1`, `VLLM_NO_USAGE_STATS=1`, `DO_NOT_TRACK=1`, `HF_HOME=/models`.

## Lỗi (root cause đã xác minh qua log)

vLLM 0.22.0 `vllm serve /models` (local dir) fail với 1 trong 2 error tùy env:

### Error 1: `MODEL_ID=/models` + `HF_HUB_OFFLINE=1`
```
pydantic ValidationError: Value error, Invalid repository ID or local
directory specified: '/models'. Provide a valid Hugging Face repository ID.
```
vLLM coi `/models` là HF repo ID, KHÔNG parse local dir.

### Error 2: `MODEL_ID=/models` + full air-gapped env (HF_HUB_OFFLINE=1 + TRANSFORMERS_OFFLINE=1 + ...)
```
huggingface_hub.errors.LocalEntryNotFoundError: Cannot find an appropriate
cached snapshot folder for the specified revision on the local disk and
outgoing traffic has been disabled. To enable repo look-ups and downloads
online, pass 'local_files_only=False' as input.
```
vLLM vẫn coi `/models` là HF repo ID, tìm HF cache snapshot folder
`models--Qwen--Qwen3-4B-AWQ/snapshots/<sha>/` → không có → fail (offline).

### Error 3 (xác minh thêm): `MODEL_ID=Qwen/Qwen3-4B-AWQ` (HF repo ID) + `HF_HUB_OFFLINE=1`
```
httpx connect_tcp hang → SIGINT → KeyboardInterrupt: terminated
```
vLLM phone home HF (metadata resolve) dù offline → HF throttle VN → hang.

## Root cause hypothesis

vLLM 0.22.0 `--model <local-dir>` parse regression: `Path(model).exists()`
check KHÔNG trigger cho `/models` (hoặc trigger nhưng fallback sang HF
repo ID resolve). User research (ChatGPT, docs vLLM 0.22) nói vLLM 0.22
SUPPORT `--model <local-dir>` + `Path.exists()` check, nhưng thực tế 0.22.0
KHÔNG work. Patch release vLLM 0.22.1 có thể fix.

## Câu hỏi research (agent cần trả lời)

1. **vLLM 0.22.0 vs 0.22.1**: 0.22.1 có fix local-dir parse regression
   không? Changelog / release notes / GitHub PR cụ thể fix `--model <local-dir>`
   parse? (search "vllm 0.22.1 release notes", "vllm local model path regression",
   GitHub vllm-project/vllm issues/PRs about `Invalid repository ID` cho
   local dir)

2. **vLLM local-dir parse logic**: trong source vLLM 0.22.0
   `vllm/transformers_utils/repo_utils.py` (hoặc `config/model.py`), logic
   nào decide `--model` là local dir vs HF repo ID? Có `Path(model).exists()`
   check? Tại sao `/models` (tồn tại, có config.json) bị coi là HF repo ID?
   (fetch source code v0.22.0 + v0.22.1, diff logic parse)

3. **HF cache structure approach**: nếu dùng `--model Qwen/Qwen3-4B-AWQ` +
   `HF_HUB_OFFLINE=1` + pre-populate HF cache
   `/models/models--Qwen--Qwen3-4B-AWQ/snapshots/<sha>/` (full snapshot
   với symlinks refs/main → sha), vLLM 0.22.0 có tìm local cache và skip
   HF phone home không? Cần `hf download --revision <sha>` preserve symlinks?
   Tool `hf` CLI có trên GitHub Actions runner không?

4. **Workaround env/flag**: có env var hoặc `--model-impl transformers`
   flag nào ép vLLM 0.22.0 treat `--model` as local dir (skip HF repo ID
   resolve)? `--model-impl transformers` có thay đổi parse logic không?
   (user research nói KHÔNG, nhưng verify trong source)

5. **vLLM version recommend**: upgrade 0.22.1 (patch) vs 0.23 (minor) vs
   downgrade 0.21 — cái nào fix local-dir parse + giữ compatibility với
   Qwen3ForCausalLM + AWQ-Marlin + modelinfo cache (PR #23558 đã merge
   trước 0.22.0 release)?

## Output mong muốn

- Confirm 0.22.1 fix local-dir parse (yes/no) + PR/issue link
- Hoặc workaround cụ thể (env/flag/cache structure) cho 0.22.0
- Recommend version + approach để ship Stage 2 với self-host vLLM (giữ design
  D2: engine path identical Stage 2→3)

## Files liên quan (nếu agent cần xem)

- `services/llm/Dockerfile` (base vllm/vllm-openai:v0.22.0, MODEL_ID, CMD)
- `infra/modules/compute/main.tf` (llm container env: MODEL_ID, HF_HUB_OFFLINE, HF_HOME, WEIGHTS_S3_URI)
- `services/scripts/fetch_weights.sh` (S3 sync → /models flat)
- `scripts/gen_vllm_modelinfo_cache.py` (PR #23558 cache cho Qwen3ForCausalLM)
- `.github/workflows/seed-weights.yml` (GHA seed S3)
