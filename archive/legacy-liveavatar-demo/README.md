# LiveAvatar Demo

Block-wise autoregressive streaming proof-of-concept implementing LiveAvatar
(arXiv 2512.04677) with mock DiT inference and real anti-drift strategies.

## Quick Start

```bash
# From the workspace root
uv sync

# Run the demo
uv run python -m implementations.liveavatar_demo.web_ui.app

# Or with custom config
uv run python -m implementations.liveavatar_demo.web_ui.app \
    --config implementations/liveavatar_demo/configs/default.yaml \
    --device cpu
```

Then open:
- **Gradio control panel**: http://localhost:7860
- **WebRTC viewer**: http://localhost:8000/static/index.html

## Architecture

```
Viewer Message → LLM Responder (mock templates)
                  ↓
              EdgeTTS (Vietnamese)
                  ↓
          Wav2Vec2 Audio Encoder (real)
                  ↓
      Mock DiT + Anti-Drift (real strategies)
                  ↓
         Streaming Output (mock VAE)
                  ↓
          WebRTC Track → Browser
```

## Anti-Drift Strategies (from LiveAvatar paper)

| Strategy | What it does | Toggle |
|----------|-------------|--------|
| History Corrupt | Add noise to KV cache at matching sigma | `enable_history_corrupt` |
| AAS | Replace sink frame with model's first output | `enable_aas` |
| Rolling RoPE | Reassign positions within rolling window | `enable_rolling_rope` |
| Rolling KV Cache | FIFO eviction, bounded memory (L=4) | Always on |

## Swapping to Real Weights

When a powerful GPU (≥48GB VRAM, FP8) is available:

1. Replace `MockAvatarGenerator` with Wan2.2-S2V-14B + LoRA
2. Replace `_mock_vae_decode` with Wan2.2 causal VAE
3. Replace `MockLLMResponder` with Qwen3-4B or API
4. Keep all anti-drift strategies unchanged — they use real tensor ops

## File Structure

```
liveavatar_demo/
├── pyproject.toml
├── configs/default.yaml
├── anti_drift/
│   ├── rolling_kv_cache.py
│   ├── history_corrupt.py
│   ├── adaptive_attention_sink.py
│   └── rolling_rope.py
├── pipelines/
│   ├── orchestrator.py
│   ├── llm_responder.py
│   ├── tts_engine.py
│   ├── audio_encoder.py
│   ├── avatar_generator.py
│   └── streaming_output.py
├── signaling/
│   ├── server.py
│   └── webrtc_track.py
├── web_ui/
│   ├── app.py
│   └── static/index.html
├── examples/
│   ├── product_catalog.yaml
│   └── reference_images/
└── tests/
    └── test_streaming_loop.py
```
