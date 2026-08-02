# LiveAvatar Cloud provider — `providers/liveavatar_cloud/`

> **Role update (2026-06-22):** `providers/liveavatar_cloud/` is one render-backend
> compatibility option. The canonical production entrypoint is **`backend.main:app`** from
> `services/product/backend_service/src/`, serving **`/api/v1`**. This folder stays as the cloud
> SDK, examples, and demo frontend; its modules are reused by the staged `core/` compatibility seam.

Standalone LiveAvatar REST integration for the VN live-commerce host. Kept
**separate** from `../archive/legacy-liveavatar-demo/` (archived mock diffusion PoC) — this
folder talks to the real hosted LiveAvatar service at `api.liveavatar.com`.

API key lives in `../.env` (`LIVEAVATAR_API_KEY`, gitignored). Verified working
against the sandbox on 2026-06-22 (FULL + LITE lifecycle both pass).

```
providers/liveavatar_cloud/
├── sdk/
│   ├── client.py        # LiveAvatarClient — REST wrapper, holds X-API-KEY (backend only)
│   ├── audio.py         # PCM resample/chunk to 16-bit 24kHz mono (LITE requirement)
├── service/
│   ├── lite_agent.py    # LiteAudioAgent — LITE audio WebSocket (agent.speak/...)
│   ├── conversation.py  # LiteConversation — viewer → LLM → TTS → avatar turn cycle
│   ├── server.py        # FastAPI token broker (FULL + LITE) — frontend-safe endpoints
│   ├── colab_server.py  # Public LITE backend for Colab + ngrok (push-to-git target)
├── examples/
│   ├── smoke_test.py
│   ├── lite_smoke_test.py
│   ├── conversation_smoke_test.py
│   ├── server_ws_smoke_test.py
│   └── colab_deploy.py
└── README.md
```

All smoke tests verified live against the sandbox on 2026-06-22:
FULL+LITE lifecycle, LITE audio (test tone → `agent.speak_ended`), full
conversation turn cycle, and the Colab backend endpoints (no secret leak).

---

## Quick start

```powershell
cd projects/ai-livestream-commerce-vn/implementations

# Live sandbox smoke tests require LIVEAVATAR_API_KEY and may contact the API.
uv run python -m providers.liveavatar_cloud.examples.smoke_test

# Standalone provider server: its public contract is /api and /ws/control/{session_id}.
uv run uvicorn providers.liveavatar_cloud.service.colab_server:app --port 8800

# Application frontend: paste the backend.main origin into frontend/lite.html; it appends /api/v1 itself.
uv run --project services/product/backend_service uvicorn backend.main:app --port 8800
```

Dependencies (already in the repo env): `requests`, `fastapi`, `uvicorn`,
`pydantic`. The SDK loads an ignored `.env` file only as a fallback; environment variables take precedence.

---

## 1b. Historical pricing snapshot (verified 2026-06-22)

The rates and account details below were recorded on 2026-06-22. They are not a
current pricing claim; check the provider documentation before budgeting. The
snapshot described credit-based, per-minute active-session billing after setup.

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

Historical account snapshot verified on 2026-06-22: **Free tier, 10 credits left** (verified via
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
- A real Vietnamese talking-host avatar replacing the mock face in `archive/legacy-liveavatar-demo`.
- Chat-driven commerce host: viewer message → `avatar.speak_response` → avatar answers about products/promos.
- Bring-your-own LLM (the planned Qwen3-4B) via `llm_configuration_id` while LiveAvatar handles ASR + TTS + video (FULL + Custom LLM).
- Bring-your-own pipeline: stream your own VN TTS (PCM 24 kHz) and let LiveAvatar only render video (LITE).
- A no-code landing-page demo via the iframe embed.

> ⚠️ Vietnamese voices: your account currently exposes **70 `en` voices, no `vi`**.
> For Vietnamese speech today, use **LITE mode** with your own VN TTS (e.g. the
> EdgeTTS `vi-VN-*` already in `archive/legacy-liveavatar-demo`), or **FULL + Custom TTS** with an
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
- **LITE** reusing the VN EdgeTTS pipeline already built in `archive/legacy-liveavatar-demo`
  (resampled to PCM 24 kHz).

We are building **LITE + self-hosted LLM + TTS** (your choice) — see §4 and §5.

---

## 4. Self-hosted models for LITE (researched 2026-06-22, slugs verified)

### LLM runtime

Use the approved OpenAI-compatible vLLM route for this project:
`cyankiwi/Qwen3.5-4B-AWQ-4bit`. Configure the core control plane with
`LLM_ENGINE=vllm` and `LLM_MODEL=cyankiwi/Qwen3.5-4B-AWQ-4bit`.

The provider's LITE mode receives server-side PCM TTS audio and renders video;
it does not prescribe a local model-loader implementation.

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
[git push]  providers/liveavatar_cloud/  ──►  [Colab cell]
                                    pip install -e . vllm pyngrok
                                    python -m providers.liveavatar_cloud.examples.colab_deploy
                                      ├─ connect the configured vLLM and TTS backends
                                      ├─ uvicorn colab_server:app  (subprocess/thread)
                                      └─ pyngrok tunnel → prints https://xxxx.ngrok-free.app
                                                                   │
[standalone provider viewer] ── paste ngrok URL ── POST /api/lite/* ───────┘
        ▲                                                          │
        └──────────── avatar VIDEO via LiveKit (WebRTC) ◄── LiveAvatar cloud
```

### Deploy steps

```python
# In a Colab cell (GPU runtime), set secrets through google.colab.userdata:
!git clone <your-repo> && cd <repo>/projects/ai-livestream-commerce-vn/implementations
!pip install -e . vllm pyngrok
# The notebook loads LIVEAVATAR_API_KEY and NGROK_AUTHTOKEN without printing them.
!python -m providers.liveavatar_cloud.examples.colab_deploy
```

Use a standalone provider viewer for this `/api` server. For `backend.main`, paste
the origin into `frontend/lite.html`; the frontend appends `/api/v1` itself.

---

## Auth model (do not mix these up)

────
X-API-KEY ──────────── Backend only. Tokens, contexts, secrets, embeds. NEVER in browser.
Bearer session_token ── Backend. start / keep-alive / stop a session.
livekit_client_token ── Frontend-safe. Joins the LiveKit room (video).
ws_url ─────────────── Backend/agent (LITE only). Streams PCM audio.
────

The `service/colab_server.py` broker enforces this split: the browser only ever receives
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
