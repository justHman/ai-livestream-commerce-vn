# Runbook: Colab T4 Deployment

Step-by-step to run the VN Live-Commerce host on a free Colab T4 GPU.

## 1. Prerequisites

- Colab account (free tier)
- GPU runtime: Runtime -> Change runtime type -> T4 GPU
- HF token (if using gated models): https://huggingface.co/settings/tokens -> accept gemma license
- LiveAvatar API key: https://liveavatar.io -> dashboard -> create key
- ngrok auth token: https://dashboard.ngrok.com -> get `NGROK_AUTHTOKEN`

## 2. Secrets setup (Colab Secrets)

Open the Colab "Secrets" panel (key icon) and set:

| Secret | Value |
|--------|-------|
| `HF_TOKEN` | Your HF user access token |
| `LIVEAVATAR_API_KEY` | LiveAvatar sandbox API key |
| `NGROK_AUTHTOKEN` | ngrok auth token |

## 3. Notebook walkthrough

### Cell 1: Clone the repo
```
REPO_URL = "https://github.com/<you>/<repo>.git"  # EDIT THIS
```

Set your actual repo URL. The notebook clones into `/content/repo` and `cd`s
to `projects/ai-livestream-commerce-vn/implementations/`.

### Cell 2: Install dependencies
Installs:
- `core/` backend package (via `-r requirements.txt` or `uv sync`)
- `llama-cpp-python` (T4 CUDA build)
- `pyngrok` (tunnel)
- `vieneu-tts` or `transformers` (TTS model)

### Cell 3: Download model weights
Downloads the LLM GGUF and TTS weights to `/content/weights/`.

**If gemma download fails** (gated, no HF token): change `LLM_REPO` to
`Qwen/Qwen3-4B-GGUF` and `LLM_FILE` to `Qwen3-4B-Q4_K_M.gguf` (Apache-2.0,
no gating).

### Cell 4: Move weights
Copies weights to `weights/llm/` and `weights/tts/` under the implementations
directory.

### Cell 5: Set env vars
Sets `RENDER_BACKEND=cloud`, `SESSION_STORE=memory`, `LLM_ENGINE=llamacpp`,
`TTS_ENGINE=vieneu`, `DIRECTOR_ENABLED=1`.

### Cell 6: Smoke test (optional)
```
uv run python -m core.tests.v1_smoke_test
```
Runs the API surface test using echo/tone stubs (no model load, 0 credits).

### Cell 7: Launch
```
uv run python -m providers.liveavatar_cloud.examples.colab_deploy
```
This runs `colab_deploy.py` which:
1. Loads LLM via `core.llm.load_engine` (llamacpp GGUF, ~10-30s)
2. Loads TTS via `core.tts.load_engine` (vieneu or transformers, ~5s)
3. Injects both into the cloud RenderBackend via `core.render.cloud.configure()`
4. Starts uvicorn serving `core.server:app` on `:8800` in a background thread
5. Opens ngrok tunnel, prints the public URL

**This cell blocks** -- it keeps the tunnel alive.

### Cell 8: Connect frontend
1. Open `frontend/lite.html` from the repo (locally or any static host).
2. Paste the ngrok URL from cell output as "Backend URL".
3. Click "Start session" -> you should see the sandbox avatar on LiveKit video.
4. Type a message in the chat input -> it routes through `/api/v1/lite/say` ->
   gemma LLM -> VieNeu TTS -> avatar speaks.

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
os.environ["LLM_ENGINE"] = "llamacpp"
os.environ["LLM_MODEL_PATH"] = "weights/llm"
os.environ["LLM_N_CTX"] = "4096"
```

**At runtime** (from the frontend dropdown):
```bash
curl -X POST <ngrok-url>/api/v1/engines/llm \
  -H "Authorization: Bearer <ADMIN_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"engine": "llamacpp", "model_path": "weights/llm", "n_ctx": 4096, "n_gpu_layers": -1}'
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
**Symptom**: `CUDA out of memory` when loading the GGUF model.

**Fixes** (in order):
1. Use a smaller context: `LLM_N_CTX=2048`
2. Partial GPU offload: `LLM_N_GPU_LAYERS=24` (leave some layers on CPU)
3. Switch to a smaller model: use Qwen3-4B (4B) instead of SeaLLMs-v3-7B (7B)
4. Switch TTS to `facebook/mms-tts-vie` (transformers, ~500MB vs vieneu ~1GB)

### ngrok auth failure
**Symptom**: `authentication failed` when starting ngrok.

**Fix**: Set `NGROK_AUTHTOKEN` in Colab Secrets. Free tier allows 1 tunnel.

### Model download failures
**Symptom**: `HTTP 403` or `401` when downloading from HF.

**Fix**: For gated models (gemma-3-4b-it): accept license on HF website, then
set `HF_TOKEN` in Colab Secrets. Alternative: use Qwen3-4B (Apache-2.0, no gating).

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
