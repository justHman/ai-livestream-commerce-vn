# Colab readiness checklist

Preflight checks before running the Colab vLLM demo.

## Environment

- [ ] Colab GPU runtime selected; vLLM requires CUDA.
- [ ] `NGROK_AUTHTOKEN` is set in Colab Secrets because the notebook opens a tunnel.
- [ ] `LIVEAVATAR_API_KEY` is set only when cloud mode is enabled.
- [ ] Preflight confirms execution inside Colab and loads secrets without printing them.
- [ ] Repository URL and branch are intentional.
- [ ] `VLLM_MODEL_ID=cyankiwi/Qwen3.5-4B-AWQ-4bit` is configured for the notebook; its launch cell exports it as `LLM_MODEL`.

## Notebook cell validation

- [ ] First code cell is preflight and contains no install, clone, or network operation.
- [ ] Preflight validates Colab imports, CUDA, configuration, and writable `OUTPUT_DIR`.
- [ ] Preflight declares `REPO_DIR` as planned before clone; it does not require it to exist.
- [ ] The demo declares no local dataset or checkpoint. Any added loader prints shape,
      dtype, size, and source.
- [ ] Provider import cell does not instantiate `LiveAvatarClient`.
- [ ] Smoke cell starts and stops a mock `/api/v1/lite` session, then writes an artifact.
- [ ] Final cell prints a compact recursive tree from `OUTPUT_DIR` and closes the log.

## Model route

- [ ] Use vLLM with `cyankiwi/Qwen3.5-4B-AWQ-4bit`.
- [ ] Keep TTS selection behind its existing runtime preset/API controls.
- [ ] Do not add local model file or checkpoint instructions to this demo.

## Troubleshooting

OOM at LLM load
: Use a smaller vLLM model, reduce concurrency, or select a larger GPU.

OOM at TTS load
: Switch to `facebook/mms-tts-vie` when the configured preset is too large.

ngrok auth failure
: Set `NGROK_AUTHTOKEN` in Colab Secrets; free tier allows one tunnel.

Model access failure
: Verify access to `cyankiwi/Qwen3.5-4B-AWQ-4bit` and the vLLM configuration.

Smoke failure
: Confirm `RENDER_BACKEND=mock`, `LLM_ENGINE=vllm`, and `TTS_ENGINE=tone`.

## Post-launch verification

- [ ] `GET /api/v1/health/ready` responds after launch.
- [ ] Smoke cell starts and stops mock `/api/v1/lite` session successfully.
- [ ] Paste the canonical backend origin into `frontend/lite.html`; it appends `/api/v1` itself.
      Do not paste `/api/v1`.
- [ ] Final output tree and backend log path are captured before shutdown.
