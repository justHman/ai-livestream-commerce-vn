# Runbook: Colab vLLM demo

Run the canonical backend FastAPI application on a Colab GPU for a development demo. This is not
an AWS deployment and does not validate a real LiveKit SFU/media path.

## Prerequisites

- Colab GPU runtime; vLLM requires CUDA.
- `NGROK_AUTHTOKEN` in Colab Secrets for the public tunnel.
- `HF_TOKEN` only if the selected Hugging Face source requires it.
- `LIVEAVATAR_API_KEY` only when `USE_CLOUD_LIVEAVATAR=True`; default mock mode
  needs no key and consumes no LiveAvatar credits.

Secrets are read through `google.colab.userdata` and never printed.

## Notebook contract

`notebooks/colab_demo.ipynb` is the canonical walkthrough:

1. Preflight checks Colab/CUDA/imports, validates configuration and writable
   `OUTPUT_DIR`, and fails before clone/install.
2. Clone checks out the configured repository and branch.
3. Install adds the editable project, vLLM, and pyngrok without creating a
   LiveAvatar client.
4. Launch uses `RENDER_BACKEND=mock`, `LLM_ENGINE=vllm`,
   `LLM_MODEL=cyankiwi/Qwen3.5-4B-AWQ-4bit`, and `TTS_ENGINE=tone`.
5. Tunnel uses `NGROK_AUTHTOKEN`.
6. Smoke starts/stops a mock `/api/v1/lite` session and writes under
   `OUTPUT_DIR`.
7. Final report prints a compact output tree and exposes shutdown cleanup.

Any future dataset/checkpoint loader must print source, shape, dtype, and size.

Paste an origin such as `https://example.ngrok.app` into the workbench console;
the page appends `/api/v1`. The standalone
The legacy standalone provider server is removed; the canonical backend serves `/api/v1`
contract.

## Engine changes

```python
os.environ["LLM_ENGINE"] = "vllm"
os.environ["LLM_MODEL"] = "cyankiwi/Qwen3.5-4B-AWQ-4bit"
os.environ["TTS_ENGINE"] = "tone"
```

Use the admin engine endpoint for an optional TTS preset only if its dependency
and model access are available. The demo does not guarantee all presets fit a
free T4.

## Health checks

```bash
curl <ngrok-url>/api/v1/health
curl <ngrok-url>/api/v1/health/live
curl <ngrok-url>/api/v1/health/ready
curl <ngrok-url>/api/v1/engines -H "Authorization: Bearer <ADMIN_API_TOKEN>"
```

## Troubleshooting

- **OOM:** choose a smaller vLLM model, reduce concurrency/context, or use a
  larger GPU; keep TTS as `tone` while isolating LLM loading.
- **ngrok authentication:** set `NGROK_AUTHTOKEN`; free tier permits one tunnel.
- **Model access:** verify access to `cyankiwi/Qwen3.5-4B-AWQ-4bit`; use
  `HF_TOKEN` only when required.
- **Provider import:** run from the cloned `implementations/` root and import
  `backend.application.clients.avatar.liveavatar_sdk`.
- **Mock route 404:** launch with `RENDER_BACKEND=mock`; mock endpoints are not
  the production media contract.
- **Cloud renderer:** use `RENDER_BACKEND=cloud_liveavatar` with
  `LIVEAVATAR_API_KEY`. Default notebook mode remains mock.
