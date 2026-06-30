# PRODUCTION.md -- Historical

> This doc is historical. See `architecture.md` for the current design.
>
> Key changes since this was written:
> - `liveavatar_api/` is now `providers/liveavatar_cloud/`
> - `liveavatar_demo/` is now `archive/legacy-liveavatar-demo/`
> - `liveavatar_api_cloud/` typo paths corrected to `providers/liveavatar_cloud/`
> - Frontend files moved to `frontend/` (from liveavatar_api/frontend/)
> - Director (clustering + FSM) implemented: `core/director/`
> - TTS engine seam implemented: `core/tts/` (VieNeu, CosyVoice, transformers adapters)
> - LLM engine seam implemented: `core/llm/` with streaming support
> - Mock renderer (`RENDER_BACKEND=mock`) implemented: `core/render/mock.py`
> - Streaming orchestrator implemented: `core/render/orchestrator.py`
> - Session locks, auth, app factory, text chunker all implemented
> - Colab bootstrap notebook uses `core.server:app` via the unified engine seams
> - EngineManager with runtime swap (`POST /engines/llm`, `/engines/tts`)

# PRODUCTION.md — Architecture & Portability (original, preserved below)

VN live-commerce AI host. This documents the production architecture, the
renderer seam, and how the SAME code runs on free Colab now and lifts to AWS
later by changing only environment variables.

## 1. Two planes (the core idea)

```
                CONTROL plane (this backend)              MEDIA plane (NOT this backend)
  Frontend ──HTTP /api/v1 + WS──►  core server  ──►  Renderer (LiveAvatar cloud / self-host)
 (browser) ◄──WS events──────────  (control)              │
     ▲                                                     │
     └──────────── WebRTC video via LiveKit ◄──────────────┘
```

- **Control plane** = `core/` FastAPI app: session lifecycle, `say`, interrupt, events. JSON + WebSocket.
- **Media plane** = avatar VIDEO. It flows renderer → LiveKit → browser **directly**. Frames never transit our backend.
- The browser only ever receives frontend-safe creds: `livekit_url` + `livekit_client_token`. Secrets (`X-API-KEY`, `session_token`, audio `ws_url`) stay backend-side.

Why split: the renderer (LiveAvatar) already delivers H.264/VP8 over WebRTC sub-second. Re-piping frames through our box adds latency + load for zero benefit. The frontend is a thin client of the API + a LiveKit token; the SWE team can swap it without touching the backend.

## 2. RenderBackend seam — cloud now, self-host later

`core/render/base.py` defines `RenderBackend` (ABC): `start / say / interrupt / stop`.
The API layer (`core/api/v1.py`) talks ONLY to this interface.

```
core/render/base.py        RenderBackend ABC  (start/say/interrupt/stop)
core/render/cloud.py       CloudRenderBackend  -> wraps providers.liveavatar_cloud (LiveAvatar cloud) [ACTIVE]
core/render/self_host.py   SelfHostRenderBackend -> future diffusion model [STUB, NotImplemented]
```

Select with `RENDER_BACKEND=cloud|self_host`. Adding the self-host renderer (Wan2.2-S2V / EchoMimic
class, multi-image anti-drift, batch-streaming on 1-2 GPUs) needs ZERO changes above the seam — verified
by the smoke test that routes to `self_host` and gets a clean NotImplementedError.

`providers/liveavatar_cloud/` is unchanged and now sits BEHIND the cloud adapter — it is "one service option", not
the product. `archive/legacy-liveavatar-demo/` (mock diffusion PoC) is untouched.

## 3. Portability — same code, Colab → AWS (env only)

```
                    Colab (now)                 AWS (later)
RENDER_BACKEND      cloud                        cloud (or self_host)
SESSION_STORE       memory (InMemory)            redis (RedisSessionStore)
process model       1 uvicorn process            N containers behind ALB (sticky sessions)
LLM/TTS             local (injected via          shared vLLM/TTS endpoint
                    core.render.cloud.configure)
public access       ngrok tunnel                 ALB + CloudFront
```

- `core/config.py` (`AppConfig.from_env`) is the single source of deployment values. Nothing hardcoded.
- `core/store.py` `SessionStore`: `InMemorySessionStore` (Colab) ↔ `RedisSessionStore` (AWS). Stores only
  JSON session metadata; the live WS/agent object stays in-process on the owning instance.
- LLM/TTS are injected (`core.render.cloud.configure(llm_fn, tts_fn)`) so backends swap without API changes.

### Sticky sessions (AWS)
A live session holds an in-process WS/agent on one instance. ALB **sticky sessions** (duration cookie or
`session_id`) must route a session's requests back to its owning instance. Cross-instance metadata lives
in Redis; the live connection does not migrate.

### GPU load balancing (AWS) — what ALB can and cannot do
ALB balances **requests across instances** (round-robin / least-outstanding-requests + sticky). It does
NOT split one request across GPUs or balance by GPU%. GPU-aware balancing happens at the inference tier:
- **vLLM continuous batching** interleaves many sessions' tokens in one GPU batch (no per-turn head-of-line blocking).
- **Autoscaler** (KEDA/HPA on DCGM GPU-util or Redis queue depth) scales vLLM replicas; a router picks a free replica.
- Tensor-parallelism only if a model can't fit one GPU (our 4B Q4 fits → use data-parallel replicas instead).

## 4. Async request model

- `start` is fast (~1-2s) → handled synchronously; returns the LiveKit token in the 200 response.
  (`core/api/v1.py` runs the blocking renderer call off the event loop via `asyncio.to_thread`.)
- `say` (each turn) → LLM→TTS→stream to renderer; control events (`speak_started/ended`) are pushed to the
  frontend over the per-session WebSocket. Audio PCM goes backend→renderer; never over HTTP, never queued
  in a shared bus (avoids realtime head-of-line blocking).
- **Barge-in** is priority-gated by the future Director (only high-score clusters interrupt), not unconditional.

## 5. KV cache & inference (production LLM)

- **KV cache** is the baseline autoregressive speedup (every engine has it): cache K,V of past tokens →
  O(n) per step instead of O(n²).
- **Prefix caching / RadixAttention** is the big lever here: the persona + product catalog system prompt is
  fixed and long → cache its KV once, reuse across every user/turn. vLLM `enable_prefix_caching`; SGLang
  RadixAttention. This is the main technical reason production uses vLLM/SGLang over llamacpp (llamacpp
  prompt cache is single-stream, no cross-user KV sharing).
- **PagedAttention** (vLLM) packs KV in pages → more concurrent sessions per GPU. **GQA** (Qwen3, gemma-3)
  shrinks KV natively. **FP8 KV cache** (vLLM `kv_cache_dtype=fp8`) fits longer context / more sessions.

### Engine + kernels (decided)
- **Demo (Colab T4, low concurrency):** llamacpp (`llama-server`, GGUF Q4_K_M) — low TTFT, low VRAM.
- **Production (many sessions):** vLLM (continuous batching + prefix cache).
- **Attention kernels:** FlashAttention-2/3 and SageAttention2 are **out of scope on T4 (Turing sm_75)** —
  FA2 needs Ampere+, FA3 is Hopper-only, Sage needs sm_80+. Noted for future higher-tier GPUs (FA2 from
  Ampere, FA3 from Hopper). On T4, let the engine handle attention; HF `transformers` would use `sdpa`.
- **torch.compile:** N/A under llamacpp; for a `transformers` path use `reduce-overhead` only with stable
  shapes (Colab is ephemeral → compile cost rarely amortizes).

## 6. Models (selectable, all verified on HF — see datasets.yaml + validation-report)

```
LLM   gemma-3-4b-it (Gemma terms, gated)  |  Qwen3-4B (Apache-2.0)   -- Q4_K_M GGUF on T4
      SeaLLMs-v3-7B-Chat (SeaLLMs terms)  -- stronger VN, ~1.6x slower
      Qwen3.5-4B (Apache-2.0)             -- latest generation
TTS   VieNeu-TTS-v2 (Apache-2.0, VN-native) [default]  |  -v3-Turbo (Apache, 48kHz)
      CosyVoice2-0.5B (Apache) -- only if true <200ms streaming is needed (finetune on CC-BY VN data)
Embed bkai vietnamese-bi-encoder (Apache, 135M) -- cluster + retrieval (no BM25)
```
Finetune path (commercial): QLoRA SFT (Unsloth → LLaMA-Factory/Axolotl) → optional DPO/SimPO.
Commercial VN SFT data: 5CD-AI ecommerce/multi-turn + aya `vie` + ura-hcmut CSConDa (all Apache, validated).

## 7. Cost (LiveAvatar credits)

FULL/Embed = 2 credits/min; **LITE = 1 credit/min** (cheaper because we run LLM+TTS). Free tier 10 credits;
sandbox = 0 credits (all dev runs on sandbox). On paid tiers a credit ≈ $0.10 → LITE ≈ $0.10/min vs FULL
≈ $0.20/min, before our own (free Colab) GPU cost.

## 8. Layout (updated)

```
implementations/
+-- core/                    # production surface (transport-agnostic)
|   +-- server.py            # FastAPI app; mounts /api/v1; env-wired
|   +-- api/v1.py            # /api/v1 routes + WS control hub
|   +-- render/{base,cloud,mock,self_host}.py
|   +-- config.py  store.py  engine_manager.py
|   +-- director/            # Director FSM + coordinator + chat queue
|   +-- tts/                 # TTS engine seam (vieneu/cosyvoice/transformers)
|   +-- llm/                 # LLM engine seam (llamacpp/vllm/sglang/hf)
|   +-- stream/chunker.py    # TextChunker (token deltas -> phrase chunks)
|   +-- tests/
+-- providers/liveavatar_cloud/   # LiveAvatar cloud SDK (behind cloud adapter)
+-- frontend/                     # lite.html, index.html
+-- notebooks/                    # bootstrap_colab.ipynb, colab_demo.ipynb
+-- archive/legacy-liveavatar-demo/  # mock diffusion PoC (archived)
+-- docs/
    +-- architecture.md
    +-- PRODUCTION.md (historical)
    +-- PLAN.md (historical)
    +-- TASKS.md (historical)
    +-- BACKEND_PRODUCTION_FIX_PLAN.md (historical)
    +-- runbook-colab.md
    +-- checklists/
```

## 9. Run

```bash
# Colab / local (cloud renderer, sandbox)
export LIVEAVATAR_API_KEY=...           # backend-only secret
export RENDER_BACKEND=cloud SESSION_STORE=memory
uv run python -m core.tests.v1_smoke_test      # free sandbox verification
uv run uvicorn core.server:app --port 8800
# Frontend: open frontend/lite.html, paste backend URL.

# Offline (mock render, no API key, no model)
RENDER_BACKEND=mock uv run uvicorn core.server:app --port 8800
```

Verified on sandbox: core `/api/v1` (health→WS→start→say→interrupt→stop, no secret leak),
legacy `providers.liveavatar_cloud` smoke still green, self_host seam returns clean NotImplemented.
