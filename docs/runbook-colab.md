# Runbook: Colab vLLM deployment

Step-by-step to run the VN Live-Commerce host on a free Colab T4 GPU.

## 1. Prerequisites

- Colab account (free tier)
- GPU runtime: Runtime -> Change runtime type -> GPU
- HF token (if using gated models): https://huggingface.co/settings/tokens -> accept gemma license
- LiveAvatar API key: https://liveavatar.io -> dashboard -> create key
- ngrok auth token: https://dashboard.ngrok.com -> get `NGROK_AUTHTOKEN`

## 2. Secrets setup (Colab Secrets)

Open the Colab "Secrets" panel (key icon) and set:

- `NGROK_AUTHTOKEN`: required because the notebook opens a public tunnel.
- `LIVEAVATAR_API_KEY`: required only when `USE_CLOUD_LIVEAVATAR=True`.
- `HF_TOKEN`: optional for model access when the selected source requires it.

The notebook reads Colab Secrets through `google.colab.userdata`; it does not print values.

## 3. Notebook walkthrough

1. **Preflight** validates that this is Colab, imports `torch`, `IPython`, and
   `google.colab`, requires CUDA for vLLM, loads optional secrets without printing
   them, validates configuration, and checks the writable output path. It lists
   planned repository paths separately because cloning has not happened yet.
2. **Clone** obtains the configured repository branch.
3. **Install** installs the editable project, vLLM, and pyngrok. The provider is
   imported without instantiating a client.
4. **Launch** starts `core.server` with `LLM_ENGINE=vllm` and
   `LLM_MODEL=cyankiwi/Qwen3.5-4B-AWQ-4bit`.
5. **Tunnel** uses the required `NGROK_AUTHTOKEN` secret.
6. **Smoke** starts then stops a mock `/api/v1/lite` session and writes a JSON
   artifact under `OUTPUT_DIR`.
7. **Final report** prints a compact recursive output tree and exposes
   `shutdown_demo()`.

The mock demo has no local dataset or checkpoint. If a dataset, tensor, or
checkpoint is added later, its loader must print shape, dtype, size, and source.

Paste the core server origin, such as `https://example.ngrok.app`, into
`frontend/lite.html`; it appends `/api/v1` itself. Do not paste an origin already
suffixed with `/api/v1`. The standalone
`providers.liveavatar_cloud.service.colab_server` has its own `/api` contract.

## 4. Switching TTS preset

The default is `vieneu-v3-turbo`. To switch:

**At startup** (change env in Cell 5):
```
os.environ["TTS_PRESET_ID"] = "transformers-mms-vi"   # or cosyvoice2, kokoro, etc.
os.environ["TTS_ENGINE"] = "transformers"
os.environ["TTS_MODEL"] = "facebook/mms-tts-vie"
```

**At runtime** (from the frontend or curl):
```bash
curl -X POST <ngrok-url>/api/v1/engines/tts/preset \
  -H "Authorization: Bearer <ADMIN_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"preset_id": "cosyvoice2"}'
```

Then load it:
```bash
curl -X POST <ngrok-url>/api/v1/engines/tts \
  -H "Authorization: Bearer <ADMIN_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"engine": "cosyvoice", "model": "FunAudioLLM/CosyVoice2-0.5B", "sample_rate": 24000}'
```

Presets: `vieneu-v3-turbo`, `vieneu-v2`, `cosyvoice2`, `kokoro`, `xtts-v2`, `transformers-mms-vi`.

## 5. Switching LLM engine

**At startup** (change env in Cell 5):
```
os.environ["LLM_ENGINE"] = "vllm"
os.environ["LLM_MODEL"] = "cyankiwi/Qwen3.5-4B-AWQ-4bit"
```

**At runtime** (from the frontend dropdown):
```bash
curl -X POST <ngrok-url>/api/v1/engines/llm \
  -H "Authorization: Bearer <ADMIN_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"engine": "vllm", "model": "cyankiwi/Qwen3.5-4B-AWQ-4bit"}'
```

## 6. Smoke metrics

Visit these endpoints to check health:

```bash
# Service info
curl <ngrok-url>/api/v1/health

# Liveness
curl <ngrok-url>/api/v1/health/live

# Readiness (loaded engines)
curl <ngrok-url>/api/v1/health/ready

# Engine status + presets
curl <ngrok-url>/api/v1/engines \
  -H "Authorization: Bearer <ADMIN_API_TOKEN>"
```

The root endpoint also shows current engine state:
```bash
curl <ngrok-url>/
```

## 7. Troubleshooting

### OOM at LLM load
**Symptom**: `CUDA out of memory` when starting vLLM.

**Fixes** (in order):
1. Use a smaller vLLM model or a larger GPU runtime.
2. Reduce vLLM concurrency or context settings.
3. Switch TTS to `facebook/mms-tts-vie` (transformers, ~500MB vs vieneu ~1GB)

### ngrok auth failure
**Symptom**: `authentication failed` when starting ngrok.

**Fix**: Set `NGROK_AUTHTOKEN` in Colab Secrets. Free tier allows 1 tunnel.

### Model download failures
**Symptom**: `HTTP 403` or `401` when downloading from HF.

**Fix**: For gated models (gemma-3-4b-it): accept license on HF website, then
set `HF_TOKEN` in Colab Secrets. Use the approved `cyankiwi/Qwen3.5-4B-AWQ-4bit` vLLM model when available.

### vieneu import error
**Symptom**: `ModuleNotFoundError: No module named 'vieneu'`.

**Fix**: Confirm `pip install vieneu` succeeded (package is `vieneu`, not the
unrelated `neuttsair`/`neutts` English upstream package). If install still
fails, switch to the transformers fallback:
- Set `TTS_ENGINE=transformers TTS_MODEL=facebook/mms-tts-vie`
- Or use `TTS_ENGINE=cosyvoice` if CosyVoice2 package is installed.

### `liveavatar_cloud` import error
**Symptom**: `ModuleNotFoundError: No module named 'providers.liveavatar_cloud'`

**Fix**: Make sure the working directory is the `implementations/` root (not
`core/` or `providers/`). The notebook should `%cd` to the implementations dir.

### `/mock/frame` returns 404
**Symptom**: The mock frame endpoint returns `{"detail":"Not Found"}`.

**Fix**: `RENDER_BACKEND=mock` must be set at startup. The mock endpoints only
exist when the active backend is MockRenderBackend. Switch to `RENDER_BACKEND=mock`
to test without LiveAvatar credits.

### Backend crash on startup
**Symptom**: uvicorn fails to start or crashes immediately.

**Fix**: Check `RENDER_BACKEND` setting. If `cloud`, ensure `LIVEAVATAR_API_KEY`
is set. If `mock`, no key needed but mock mode requires `LLM_ENGINE=none` or a
real LLM engine loaded.
