# LiveAvatar API Integration — `liveavatar_api/`

> **Role update (2026-06-22):** `liveavatar_api/` is no longer the product root — it is now **one
> render-backend option** (LiveAvatar cloud) behind the `core/` RenderBackend seam. The production
> entrypoint is **`core.server:app`** serving **`/api/v1`** (see `../PRODUCTION.md`). A future
> self-host diffusion renderer plugs in as a second backend without changing the API. This folder
> stays as the cloud SDK + examples + demo frontend; its modules are reused unchanged by
> `core/render/cloud.py`.

Standalone LiveAvatar REST integration for the VN live-commerce host. Kept
**separate** from `../liveavatar_demo/` (the self-built mock pipeline) — this
folder talks to the real hosted LiveAvatar service at `api.liveavatar.com`.

API key lives in `../.env` (`LIVEAVATAR_API_KEY`, gitignored). Verified working
against the sandbox on 2026-06-22 (FULL + LITE lifecycle both pass).

```
liveavatar_api/
├── backend/
│   ├── client.py        # LiveAvatarClient — REST wrapper, holds X-API-KEY (backend only)
│   ├── audio.py         # PCM resample/chunk to 16-bit 24kHz mono (LITE requirement)
│   ├── lite_agent.py    # LiteAudioAgent — LITE audio WebSocket (agent.speak/...)
│   ├── conversation.py  # LiteConversation — viewer → LLM → TTS → avatar turn cycle
│   ├── server.py        # FastAPI token broker (FULL + LITE) — frontend-safe endpoints
│   └── colab_server.py  # Public LITE backend for Colab + ngrok (push-to-git target)
├── frontend/
│   ├── index.html       # FULL-mode LiveKit test viewer
│   └── lite.html        # LITE viewer → talks to the Colab backend
├── examples/
│   ├── smoke_test.py              # FULL+LITE lifecycle (token/start/stop)
│   ├── lite_smoke_test.py         # LITE audio WebSocket path (test tone)
│   ├── conversation_smoke_test.py # full LITE turn cycle (stub LLM+TTS)
│   └── colab_deploy.py            # Colab launcher: load models + serve + ngrok
├── requirements.txt
└── README.md
```

All smoke tests verified live against the sandbox on 2026-06-22:
FULL+LITE lifecycle, LITE audio (test tone → `agent.speak_ended`), full
conversation turn cycle, and the Colab backend endpoints (no secret leak).

---

## Quick start

```powershell
cd projects/ai-livestream-commerce-vn/implementations

# 1. Verify the key + run full sandbox lifecycle (free, no credits)
python -m liveavatar_api.examples.smoke_test

# 2. Start the backend token broker (holds the API key)
uv run uvicorn liveavatar_api.backend.server:app --port 8800
#   or: python -m liveavatar_api.backend.server

# 3. Open the test viewer
#   Serve frontend/index.html (any static server) and click ▶ Start (FULL).
python -m http.server 8901 --directory liveavatar_api/frontend
#   -> http://127.0.0.1:8901
```

Dependencies (already in the repo env): `requests`, `fastapi`, `uvicorn`,
`pydantic`. No `python-dotenv` needed — `client.py` has a minimal `.env` loader.

---

## 1b. Pricing — and why LITE is cheaper

Billing is **credit-based, per minute of active session** (setup time before the
client token is free; the meter starts when the session actually begins). Polled
every minute across all concurrent sessions.

────
FULL / Embed ── **2 credits / minute**
LITE ───────── **1 credit / minute**  ← half price, because YOU run LLM + TTS
────

So yes — LITE is literally **2× cheaper per minute** on LiveAvatar, precisely
because you supply STT/LLM/TTS and LiveAvatar only renders video. The tradeoff:
you pay for your own compute (the free Colab GPU here) and own the audio wiring.

Subscription tiers (from docs.liveavatar.com/docs/faq/credits):

────
Free ───── $0/mo ───── 10 credits ─────── no overage
Starter ── $19/mo ──── 150 (+10 bonus) ── overage $0.12/min
Pro ────── $99/mo ──── 1,000 (+10) ────── overage $0.10/credit
Scale ──── $475/mo ─── 5,000 (+10) ────── overage $0.10/credit
────

Your account right now: **Free tier, 10 credits left** (verified via
`GET /v1/users/credits`). At LITE's 1 credit/min that's ~10 minutes of live
avatar; FULL would burn it in ~5. Sandbox sessions cost **0 credits**, so do all
development against sandbox (as the smoke tests do).

Rough cost-per-minute once paid: on Pro/Scale a credit is ~$0.10, so LITE ≈
**$0.10/min** vs FULL ≈ **$0.20/min** — before your own GPU/LLM/TTS cost (which
on free Colab is $0). For a long-running live-commerce stream, LITE + free Colab
is the cheapest path by a wide margin.

---

## 2. What the API key unlocks

The key (`LIVEAVATAR_API_KEY`) is a **backend secret** that authenticates every
`X-API-KEY` call. With it you can drive the entire hosted avatar service. From
the docs (docs.liveavatar.com) + verified probes against your account:

Account discovery
   `GET /v1/avatars` (your account: 0 custom avatars yet — use sandbox), `GET /v1/voices` (70 voices, all `en` currently).

Contexts (avatar "brain")
   `POST /v1/contexts` — name + system prompt + opening line + optional reference URLs. Reusable across sessions. Verified working with Vietnamese prompt text.

Sessions
   `POST /v1/sessions/token` → `/start` → `/keep-alive` → `/stop`. The one endpoint that configures everything (mode, avatar, persona, video quality, STT/TTS provider, custom LLM, transport).

Secrets + custom providers
   `POST /v1/secrets` to store your own LLM / ElevenLabs keys, then `POST /v1/llm-configurations` (any OpenAI-compatible LLM) or `POST /v1/voices/third_party` (import an ElevenLabs voice).

Embeds
   `POST /v2/embeddings` — get an `<iframe>` for a no-code avatar on any web page.

Session memory (added 2026-06-01)
   CRUD on memories; attach `prev_session_id` / `session_memory_id` so a new session remembers a previous one.

Third-party realtime agents (LITE)
   Bridge an ElevenLabs Conversational AI agent, OpenAI Realtime, or Gemini Realtime directly to the avatar.

What you can control per session (from the `/v1/sessions/token` schema):
avatar_id, voice_id + voice_settings (ElevenLabs / Fish Audio), context_id,
language, STT provider (`deepgram` / `assembly_ai` / `gladia` / `elevenlabs`),
custom LLM via `llm_configuration_id`, `interactivity_type`
(`CONVERSATIONAL` / `PUSH_TO_TALK`), `video_quality` + `video_encoding`
(`VP8` / `H264`), 1080p (`is_1080p`), `max_session_duration`, session memory,
and `dynamic_variables` to fill `${var}` placeholders in the prompt.

Recent changelog highlights: 1080p avatars, OpenAI/Gemini realtime voices,
session memory, ElevenLabs JP text normalization (May–Jun 2026).

### What you can build for THIS project
- A real Vietnamese talking-host avatar replacing the mock face in `liveavatar_demo`.
- Chat-driven commerce host: viewer message → `avatar.speak_response` → avatar answers about products/promos.
- Bring-your-own LLM (the planned Qwen3-4B) via `llm_configuration_id` while LiveAvatar handles ASR + TTS + video (FULL + Custom LLM).
- Bring-your-own pipeline: stream your own VN TTS (PCM 24 kHz) and let LiveAvatar only render video (LITE).
- A no-code landing-page demo via the iframe embed.

> ⚠️ Vietnamese voices: your account currently exposes **70 `en` voices, no `vi`**.
> For Vietnamese speech today, use **LITE mode** with your own VN TTS (e.g. the
> EdgeTTS `vi-VN-*` already in `liveavatar_demo`), or **FULL + Custom TTS** with an
> ElevenLabs multilingual voice. Re-check `GET /v1/voices` later for native `vi`.

---

## 3. Modes — FULL vs LITE vs Embed

────
FULL mode ─────────────────────────────────────
What it does ── LiveAvatar runs the WHOLE stack: ASR → LLM → TTS → video
You provide ── API key + context (prompt). Optionally your own LLM/TTS.
Transport ── LiveKit room (video + audio + data channels)
Events ── LiveKit data channels: `avatar.*` (send) / `user.*`,`avatar.*` (receive)
Audio ── LiveAvatar handles it. You never touch PCM.
Cost ── 2 credits / minute
Best when ── You have NO real AI stack (our case: mock LLM, no STT)
Effort ── Lowest — configure + ship; SDK joins the room

────
LITE mode ─────────────────────────────────────
What it does ── LiveAvatar renders VIDEO ONLY from audio you stream
You provide ── Your own STT + LLM + TTS; PCM 16-bit **24 kHz** mono audio
Transport ── LiveKit room (frontend, video) + WebSocket `ws_url` (backend, audio)
Events ── WebSocket: `agent.speak`/`agent.speak_end`/`agent.interrupt`/listening
Audio ── YOU stream PCM chunks; wrong sample rate = garbled, no error
Cost ── 1 credit / minute
Best when ── You already have a working pipeline you want to keep
Effort ── Higher — wire your turn loop into the WS protocol + audio format

────
Embed mode ────────────────────────────────────
What it does ── No-code avatar on a web page (dashboard / one API call)
You provide ── API key + avatar + context → `<iframe>` snippet
Transport ── Hosted iframe; nothing to run
Events ── None (configure via dashboard; read transcripts via API)
Audio ── Fully managed
Cost ── Same per-minute billing; simplest to deploy
Best when ── Landing-page demo, support widget, zero frontend code
Effort ── Lowest possible — paste an iframe

### Add-ons (extend a base mode)
- **FULL + Custom LLM** — your OpenAI-compatible model (Qwen3-4B), LiveAvatar keeps ASR + TTS + video.
- **FULL + Custom TTS** — your ElevenLabs voice (route to native-quality / multilingual VN).
- **FULL + Push-to-Talk** — explicit mic control (`interactivity_type: PUSH_TO_TALK`).
- **LITE + ElevenLabs Agent** — bridge an ElevenLabs Conversational AI agent (hybrid: configured LITE but uses FULL's LiveKit event system).
- **LITE + OpenAI/Gemini Realtime** — bridge a realtime voice model.
- **LITE + BYO WebRTC** — use your own LiveKit or Agora infra instead of LiveAvatar's.

### Recommendation for this project
Start **FULL (sandbox)** to validate the talking avatar end-to-end (done — smoke
test passes). Then, because you need **Vietnamese** + a **custom LLM (Qwen3-4B)**,
the production target is either:
- **FULL + Custom LLM + Custom TTS** (ElevenLabs multilingual VN voice), or
- **LITE** reusing the VN EdgeTTS pipeline already built in `liveavatar_demo`
  (resampled to PCM 24 kHz).

We are building **LITE + self-hosted LLM + TTS** (your choice) — see §4 and §5.

---

## 4. Self-hosted models for LITE (researched 2026-06-22, slugs verified)

### LLM — fastest open models for VN on free Colab

Your hunch "fast = MoE" is **half right**. MoE lowers compute *per token* (only a
few experts fire), so on a **big GPU (A100)** an MoE like Qwen3-30B-A3B is fastest.
But on a **free Colab T4 (16GB)** the *entire* expert set must still fit in VRAM,
so a 30B MoE OOMs — a small **dense** model is faster there. Pick by the GPU you get.

────
SeaLLMs/SeaLLMs-v3-7B-Chat ── 7.6B dense ── ⭐ best VN on T4 (SEA-tuned) ── 4-bit ~5GB ── vLLM/GGUF
google/gemma-3-4b-it ──────── 4B dense ──── lowest latency on T4, VN OK ── 4-bit ~3GB ── vLLM/GGUF
Qwen/Qwen3-30B-A3B-Instruct-2507 ── 30.5B/3.3B MoE ── best when you GET an A100 ── needs A100 ── vLLM
deepseek-ai/DeepSeek-V2-Lite-Chat ── 15.7B/2.4B MoE ── only MoE that fits T4, VN weaker ── 4-bit ~9GB ── vLLM
────

Default pick: **SeaLLMs-v3-7B-Chat** on T4 (strongest Vietnamese that runs
real-time); upgrade to **Qwen3-30B-A3B (MoE)** only when Colab gives you an A100.
Serve with **vLLM** (or SGLang — its RadixAttention caches the repeated system
prompt, ideal for a fixed live-commerce persona). `colab_deploy.py` defaults to
SeaLLMs via transformers+4bit; swap to vLLM for higher throughput.

> The earlier proposal mentioned Qwen3-4B — there is **no `Qwen3-4B` MoE**; the
> small Qwen3 is dense and the MoE variant is `Qwen3-30B-A3B`. SeaLLMs-v3-7B beats
> a 4B dense model on Vietnamese, so it's the better same-class choice.

### TTS — fastest open VN models (no fine-tuning needed)

────
contextboxai/Kokoro-Vietnamese ── ⭐ default ── 82M, **apache-2.0** (commercial OK), 24kHz native, runs on CPU/T4. No voice-clone.
capleaf/viXTTS ────────────────── best VN quality + zero-shot voice-clone, 24kHz, streaming. License CPML = **non-commercial**.
hynt/ZipVoice-Vietnamese-2500h ── very fast (flow-matching), trained on ~2500h. CC-BY-NC (non-commercial).
dangvansam/viet-tts ───────────── streaming OpenAI-style server, vi native. Weights CC-BY-NC; needs Docker/Linux.
────

Default pick: **Kokoro-Vietnamese** — clean apache-2.0 license, light, native
24 kHz (no resample), runs even on CPU. If you need voice-clone / higher quality
and the project stays **research/non-commercial**, use **viXTTS**.
`colab_deploy.py` defaults to Kokoro-Vietnamese.

### The "FPT + Vin opened a huge VN dataset" rumor — corrected

There is **no recent joint FPT+Vin mega-dataset**. That conflates two old, small
releases: **FPT Open Speech (FOSD)** ≈ 100h (2018, mirror `doof-ferb/fpt_fosd`)
and **VinBigData VLSP-2020** ≈ 100h (2020, mirror `doof-ferb/vlsp2020_vinai_100h`).
The genuinely *large + recent* VN speech corpora come from academia/startups, not
FPT/Vin: **`thivux/phoaudiobook`** (~1M–10M samples, gated), **`capleaf/viVoice`**
(the data behind viXTTS, gated), **`linhtran92/viet_bud500`** (~500h), and
**`dolly-vn/dolly-audio-1000h-vietnamese`** (~1000h). Good news: you **don't need
to fine-tune** — viXTTS / Kokoro-VN / ZipVoice-VN already ship VN checkpoints.

> License gate for production: viXTTS, ZipVoice-VN, F5-TTS-VN, viet-tts are all
> **non-commercial**. Only **Kokoro-Vietnamese (apache-2.0)** is safe to sell. If
> this becomes a commercial product, default to Kokoro-VN (or buy a commercial TTS).

---

## 5. Colab + ngrok deployment architecture

### The question you asked: one server with UI, or split frontend/backend?

**Split them.** Reasons, specific to LITE mode:

1. **Video never touches your backend.** In LITE, LiveAvatar streams the avatar
   video to the *browser* directly over LiveKit/WebRTC. Your Colab server only
   (a) brokers session tokens and (b) pushes your TTS PCM to LiveAvatar's audio
   WebSocket. So there are **no frames to serve** from your box — embedding a UI
   in it buys nothing.
2. **Colab + ngrok is ephemeral.** The ngrok URL changes every run. A static
   frontend (host anywhere, or open the file locally) that takes the backend URL
   as input is far easier than rebuilding a bundled UI each session.
3. **Clean security boundary.** Backend holds `X-API-KEY` + `session_token` +
   `ws_url`; frontend only ever gets `livekit_url` + `livekit_client_token`.

### Request/response protocol (the contract between the two)

You do **NOT** stream frames over HTTP. The only HTTP is a tiny JSON API; video
arrives at the browser via LiveKit. Contract (implemented in `colab_server.py`):

────
POST /api/lite/start ── body `{is_sandbox}` ── returns `{session_id, livekit_url, livekit_client_token}`. Browser renders avatar **video** from the LiveKit fields.
POST /api/lite/say ──── body `{session_id, text}` ── server runs LLM(text)→TTS→streams PCM to the avatar. Avatar speaks; browser sees it on the LiveKit track. Returns `{ok, reply}` — **no audio/video over HTTP**.
POST /api/lite/stop ─── body `{session_id}` ── ends the session.
GET  /api/health ────── liveness + whether the API key loaded.
────

So the "output of the API server" is **JSON only** (session tokens + text
replies). Frames/audio flow out-of-band: video LiveAvatar→browser (WebRTC),
audio yourTTS→LiveAvatar (WebSocket, server-side). No per-frame HTTP streaming.

### Flow

```
[git push]  liveavatar_api/  ──►  [Colab cell]
                                    pip install -r requirements.txt
                                    python -m liveavatar_api.examples.colab_deploy
                                      ├─ load SeaLLMs (LLM) + Kokoro-VN (TTS) on GPU
                                      ├─ uvicorn colab_server:app  (subprocess/thread)
                                      └─ pyngrok tunnel → prints https://xxxx.ngrok-free.app
                                                                   │
[frontend/lite.html] ── paste ngrok URL ── POST /api/lite/* ───────┘
        ▲                                                          │
        └──────────── avatar VIDEO via LiveKit (WebRTC) ◄── LiveAvatar cloud
```

### Deploy steps

```python
# In a Colab cell (GPU runtime):
!git clone <your-repo> && cd <repo>/projects/ai-livestream-commerce-vn/implementations
!pip install -r liveavatar_api/requirements.txt torch transformers accelerate bitsandbytes kokoro
import os; os.environ["LIVEAVATAR_API_KEY"] = "..."   # or upload .env
os.environ["NGROK_AUTHTOKEN"] = "..."                  # from ngrok dashboard
!python -m liveavatar_api.examples.colab_deploy        # prints the public URL
```

Then open `frontend/lite.html`, paste the ngrok URL, click **Start session**.

---

## Auth model (do not mix these up)

────
X-API-KEY ──────────── Backend only. Tokens, contexts, secrets, embeds. NEVER in browser.
Bearer session_token ── Backend. start / keep-alive / stop a session.
livekit_client_token ── Frontend-safe. Joins the LiveKit room (video).
ws_url ─────────────── Backend/agent (LITE only). Streams PCM audio.
────

The `server.py` broker enforces this split: the browser only ever receives
`livekit_url` + `livekit_client_token`; the `session_token` and `ws_url` stay
server-side, keyed by `session_id`.

## Gotchas (carried from the skill guides)
1. **No `context_id` in FULL = silent avatar.** Streams video, ignores speech, no error.
2. **Wrong auth on `/sessions/start`.** Use `Bearer <session_token>`, not `X-API-KEY`.
3. **API key in frontend = leak.** Only the broker holds it.
4. **LITE audio is PCM 16-bit / 24 kHz / mono / base64.** Wrong rate = garbled, no error.
5. **5-min timeout.** Send keep-alive every 2–3 min.
6. **Sandbox avatar** `dd73ea75-1218-4ef3-92ce-606d5f7fbc0a` (sessions) — free ~1-min, no credits. Embed sandbox avatar differs: `65f9e3c9-d48b-4118-b73a-4ae2e3cbb8f0`.

Debugging: see the `liveavatar-debug` skill for symptom-based troubleshooting.
