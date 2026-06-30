# Colab Readiness Checklist

Pre-flight checks before running on a Colab T4 GPU runtime.

## Environment

- [ ] GPU runtime selected (Runtime -> Change runtime type -> T4 GPU)
- [ ] `HF_TOKEN` set in Colab Secrets (userdata) for gated models (gemma-3-4b-it)
- [ ] `LIVEAVATAR_API_KEY` set in Colab Secrets
- [ ] `NGROK_AUTHTOKEN` set in Colab Secrets
- [ ] Git repo URL updated in notebook cell 2 (`REPO_URL`)
- [ ] Model license accepted on HF (if using gemma: visit HF repo, accept terms)

## Notebook cell validation

| Cell | Expects |
|------|---------|
| 1 (clone) | REPO_URL is a real URL, not `<you>/<repo>` |
| 4 (install) | `cmake` + `llama-cpp-python` install without error |
| 6 (weights) | Gemma terms accepted or Qwen alternative used; disk space >= 3GB |
| 10 (env) | All secrets load from `userdata` without KeyError |
| 12 (smoke) | `v1_smoke_test` passes (uses echo/tone stubs, 0 credits) |
| 14 (launch) | Models load without OOM (T4 15GB VRAM, ~3GB for 4B Q4 + ~1GB for TTS) |

## Model options

| Model | VRAM | Notes |
|-------|------|-------|
| gemma-3-4b-it Q4_K_M | ~3GB | Gated, needs HF_TOKEN |
| Qwen3-4B Q4_K_M | ~3GB | Apache-2.0, no gating |
| Qwen3.5-4B Q4_K_M | ~3GB | Apache-2.0, no gating |
| VieNeu-TTS-v2 | ~1GB | Apache-2.0, VN-native |
| facebook/mms-tts-vie | ~500MB | Apache-2.0, offline-friendly |

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| OOM at LLM load | Not enough VRAM | Switch to 4B Q4_K_M or `LLM_N_GPU_LAYERS=24` (partial offload) |
| OOM at TTS load | TTS model too large | Switch to `facebook/mms-tts-vie` (transformers, ~500MB) |
| ngrok auth failure | No `NGROK_AUTHTOKEN` | Set in Colab Secrets; free tier allows 1 tunnel |
| gemma load fails | Not gated | Use Qwen3-4B instead (no gating) |
| Smoke test fails | Wrong env or dependency | Check `LLM_ENGINE=none TTS_ENGINE=tone` are set |
| `vieneu` import fails | Package not installed | Switch to `TTS_ENGINE=transformers TTS_MODEL=facebook/mms-tts-vie` |

## Post-launch verification

- [ ] `GET /health` returns 200 with `render_backend: "cloud"`
- [ ] `GET /health/ready` returns `status: "ready"` (or has clear error if not)
- [ ] `POST /lite/start` with `is_sandbox: true` returns a session_id (0 credits)
- [ ] Frontend `lite.html` loads and connects to ngrok URL
- [ ] LiveKit video renders in the frontend video element
