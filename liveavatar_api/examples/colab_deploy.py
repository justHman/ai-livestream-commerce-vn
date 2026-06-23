"""Colab launcher — run the production core /api/v1 backend on a free Colab GPU + ngrok.

Paste this into a Colab cell (or run as a script). It:
  1. installs deps + the LLM/TTS stacks
  2. loads the LLM (gemma-3-4b / Qwen3-4B / SeaLLMs) + VN TTS on the GPU
  3. injects them into the cloud RenderBackend (core.render.cloud.configure)
  4. starts `core.server:app` (serves /api/v1) in a background thread
  5. opens an ngrok tunnel and prints the public URL

Hand the printed ngrok URL to the static frontend (frontend/lite.html)
as its "Backend URL". The browser talks JSON to /api/v1 and renders the
avatar VIDEO directly from LiveKit — frames never transit this server.

NOTE: This is a template. The model-loading functions are written to be
correct but are guarded so the file imports cleanly off-GPU. Real model
calls only run when you execute main() on Colab with a GPU.
"""

from __future__ import annotations

import os
import threading
import time


# ── 1. LLM backend (gemma-3-4b-it / Qwen3-4B, Q4_K_M GGUF via llama.cpp) ──

def build_llm():
    """Return an llm_fn(text)->reply backed by a Q4_K_M GGUF via llama.cpp.

    Models (confirmed): Qwen/Qwen3-4B-GGUF (Apache-2.0) or
    google/gemma-3-4b-it GGUF (Gemma terms). The bootstrap notebook
    downloads the GGUF into weights/llm/. llama.cpp keeps VRAM low on T4
    and gives low first-token latency for 1-2 concurrent users (demo).
    For many concurrent sessions in production, swap to vLLM + prefix cache.
    """
    import glob
    import os

    from llama_cpp import Llama  # pip install llama-cpp-python

    model_dir = os.environ.get("LLM_GGUF_DIR", "weights/llm")
    ggufs = sorted(glob.glob(os.path.join(model_dir, "*.gguf")))
    if not ggufs:
        raise FileNotFoundError(
            f"No .gguf in {model_dir}. Run the bootstrap notebook steps 3-4 "
            "(download Qwen3-4B / gemma-3-4b Q4_K_M GGUF)."
        )
    model_path = ggufs[0]
    print(f"[llm] loading {model_path}")
    llm = Llama(
        model_path=model_path,
        n_ctx=int(os.environ.get("LLM_N_CTX", "4096")),
        n_gpu_layers=int(os.environ.get("LLM_N_GPU_LAYERS", "-1")),  # -1 = all on GPU
        verbose=False,
    )

    system = (
        "Bạn là MC bán hàng livestream tiếng Việt cho mỹ phẩm. "
        "Trả lời ngắn gọn, nhiệt tình, tập trung sản phẩm và khuyến mãi."
    )

    def llm_fn(user_text: str) -> str:
        out = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_text},
            ],
            max_tokens=128,
            temperature=0.7,
        )
        return out["choices"][0]["message"]["content"].strip()

    return llm_fn


# ── 2. TTS backend (model-agnostic via core.tts registry) ───────────────

def build_tts():
    """Return a tts_fn(text)->(pcm16_bytes, sample_rate) via the core.tts seam.

    Model is selected by config — swap by changing TTS_ENGINE / TTS_WEIGHTS,
    NOT by rewriting code (no per-model import here). Adapters live in
    core/tts/adapters/ and wrap each model's official runtime.

    Default: VieNeu-TTS-v2 (Apache-2.0, Vietnamese-native). Alternatives:
    TTS_ENGINE=kokoro|cosyvoice|xtts. Falls back to the offline 'tone' engine
    if the selected model's deps are missing (keeps the server runnable).
    """
    import os

    from core.tts import load_engine, to_tts_fn

    cfg = {
        "engine": os.environ.get("TTS_ENGINE", "vieneu"),
        "weights_path": os.environ.get("TTS_WEIGHTS", "pnnbao-ump/VieNeu-TTS-v2"),
        "device": os.environ.get("TTS_DEVICE", "cuda"),
        "sample_rate": int(os.environ.get("TTS_SAMPLE_RATE", "24000")),
        "ref_audio": os.environ.get("TTS_REF_AUDIO") or None,
    }
    try:
        engine = load_engine(cfg)
        print(f"[tts] engine={engine.name} sr={engine.sample_rate}")
    except Exception as exc:
        print(f"[tts] '{cfg['engine']}' unavailable ({type(exc).__name__}: {exc}); "
              "falling back to offline 'tone' engine.")
        engine = load_engine({"engine": "tone"})

    return to_tts_fn(engine, voice=cfg.get("ref_audio"))


# ── 3. Serve + tunnel ─────────────────────────────────────────────────────

def serve_background(port: int = 8800) -> None:
    import uvicorn

    from core.server import app

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

    cloud.configure(llm_fn=llm_fn, tts_fn=tts_fn)
    print("[colab] serving core /api/v1 on :8800")
    serve_background(8800)

    url = open_tunnel(8800)
    print(f"\n=== PUBLIC BACKEND URL ===\n{url}\n")
    print("Paste this into frontend/lite.html as the Backend URL.")
    # Keep the cell alive.
    while True:
        time.sleep(60)


if __name__ == "__main__":
    main()
