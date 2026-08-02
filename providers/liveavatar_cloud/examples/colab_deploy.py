"""Colab launcher — run the canonical backend /api/v1 application on a free Colab GPU + ngrok.

Paste this into a Colab cell (or run as a script). It:
  1. installs deps + the LLM/TTS stacks
  2. loads the LLM + VN TTS via the unified engine seams (core.llm / core.tts)
  3. injects them into the cloud RenderBackend (core.render.cloud.configure)
  4. starts `backend.main:app` (serves /api/v1) in a background thread
  5. opens an ngrok tunnel and prints the public URL

Hand the printed ngrok URL to the static browser console (frontend/lite.html)
as its "Backend URL". The browser talks JSON to /api/v1 and renders the
avatar VIDEO directly from LiveKit — frames never transit this server.

Models are selected by env vars — swap model = change string, not code:
  LLM_ENGINE=llamacpp  LLM_MODEL=Qwen/Qwen3-4B-GGUF
  TTS_ENGINE=transformers  TTS_MODEL=facebook/mms-tts-vie
  (or TTS_ENGINE=vieneu for VN-native NeuTTS — needs the vieneu package)

NOTE: This is a template. The model-loading functions are written to be
correct but are guarded so the file imports cleanly off-GPU. Real model
calls only run when you execute main() on Colab with a GPU.
"""

from __future__ import annotations

import os
import threading
import time


# ── 1. LLM backend (via core.llm unified seam) ──────────────────────────

def build_llm():
    """Build an LLMEngine via core.llm.load_engine from env config.

    Default on Colab T4: llama.cpp GGUF (Q4_K_M, ~3GB VRAM, low TTFT).
    For production (many sessions): LLM_ENGINE=vllm + LLM_MODEL=Qwen/Qwen3-4B-Instruct.

    The persona system prompt is set via LLM_SYSTEM_PROMPT (or the default
    in core.config._DEFAULT_PERSONA). It is prepended to every call by
    to_llm_fn(), so the cloud backend still gets a simple (text)->str callable.
    """
    from core.config import LLMConfig
    from core.llm import load_engine, to_llm_fn

    cfg = LLMConfig.from_env()
    if cfg.engine in ("none", "", None):
        # Sensible Colab default if the user didn't set LLM_ENGINE
        cfg.engine = "llamacpp"
        cfg.model_path = cfg.model_path or os.environ.get("LLM_GGUF_DIR", "weights/llm")

    print(f"[llm] loading engine={cfg.engine} model={cfg.model or cfg.model_path}")
    engine = load_engine(cfg.to_engine_cfg())
    print(f"[llm] ready: {engine.name}")
    return to_llm_fn(engine, system_prompt=cfg.system_prompt,
                     max_tokens=cfg.max_tokens, temperature=cfg.temperature)


# ── 2. TTS backend (via core.tts unified seam) ──────────────────────────

def build_tts():
    """Build a TTSEngine via core.tts.load_engine from env config.

    Default: transformers (facebook/mms-tts-vie) — unified HF API, swap model
    by changing TTS_MODEL. Alternatives: TTS_ENGINE=vieneu (VN-native NeuTTS)
    or TTS_ENGINE=cosyvoice (streaming). Falls back to offline 'tone' engine
    if the selected model's deps are missing.
    """
    from core.config import TTSConfig
    from core.tts import load_engine, to_tts_fn

    cfg = TTSConfig.from_env()
    if cfg.engine in ("tone", "", None):
        cfg.engine = "transformers"
        cfg.model = cfg.model or "facebook/mms-tts-vie"

    print(f"[tts] loading engine={cfg.engine} model={cfg.model}")
    try:
        engine = load_engine(cfg.to_engine_cfg())
        print(f"[tts] ready: {engine.name} sr={engine.sample_rate}")
    except Exception as exc:
        print(f"[tts] '{cfg.engine}' unavailable ({type(exc).__name__}: {exc}); "
              "falling back to offline 'tone' engine.")
        engine = load_engine({"engine": "tone"})

    return to_tts_fn(engine, voice=cfg.ref_audio)


# ── 3. Serve + tunnel ─────────────────────────────────────────────────────

def serve_background(port: int = 8800) -> None:
    import uvicorn

    from backend.main import app

    cfg = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    time.sleep(2)


def open_tunnel(port: int = 8800) -> str:
    from pyngrok import ngrok  # pip install pyngrok

    token = os.environ.get("NGROK_AUTHTOKEN")
    if token:
        ngrok.set_auth_token(token)
    public = ngrok.connect(port, "http")
    return public.public_url


def main() -> None:
    from core.render import cloud

    print("[colab] loading LLM...")
    llm_fn = build_llm()
    print("[colab] loading TTS...")
    tts_fn = build_tts()

    cloud.configure(llm=llm_fn, tts=tts_fn)
    print("[colab] serving canonical backend /api/v1 on :8800")
    serve_background(8800)

    url = open_tunnel(8800)
    print(f"\n=== PUBLIC BACKEND URL ===\n{url}\n")
    print("Paste this into frontend/lite.html as the canonical backend URL.")
    # Keep the cell alive.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
