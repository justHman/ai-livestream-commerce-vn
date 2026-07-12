# Scope Update — LLM/TTS/Avatar Engines + Architecture (v2.0)

> Status: **CONFIRMED** 2026-07-10 (engines) + AWS stack 2026-07-11.
> Supersedes v1.x. Companions: `brief-for-confirmation.md`, `aws-architecture.md`, `scope-tts-engines.md`.
> Implementation plans: `../plans/00-implement-aws-stack.md`, `../plans/01-app-feature-backlog.md`.

## 1. LLM engine

**llama.cpp / GGUF removed from active use.** vLLM base only, both MVP and prod.

| Engine | Model | Format | Params | Weight | VRAM runtime | Notes |
|---|---|---|---|---:|---:|---|
| vLLM 0.22.0 (stable) | `cyankiwi/Qwen3.5-4B-AWQ-4bit` | AWQ INT4 thuần | 4.7B | ~2.5 GB | ~6 GB | Marlin kernel optimized for vLLM 0.22.0 |

- **Why AWQ INT4 thuần** (not `cyankiwi/Qwen3.5-4B-AWQ-INT8-INT4`): vLLM 0.22.0 Marlin kernel is purpose-built for INT4 — best compat + speed. INT8-INT4 (mixed, keeps embedding/lm_head INT8) is a candidate only if quality drop is measured AND it passes vLLM compat (task #48 benchmark before locking prod).
- **Model type**: tagged `image-text-to-text` (multimodal) but runs **text-only** via `--language-model-only`. Base `Qwen/Qwen3.5-4B` is chat/instruct → text-only chat completions work.
- **Prefix caching — 2 layers**:
  - **Layer 1 (P1, built-in, 0 cost)**: `--enable-prefix-caching` flag. Works within 1 replica. Classic use case: long system prompt (persona host + product catalog) repeated identically across every viewer message → prefix cached, first-token latency drops dramatically. **On by default, always.**
  - **Layer 2 (P4 scale, env-togglable `LMCACHE_ENABLED`)**: LMCache MP mode for cross-replica KV-cache sharing. When scale out to 2+ replicas, replica B reuses prefix cache from replica A via lmcache-server. Config below.
- **Scaling**: Data Parallelism (replica) via vLLM continuous batching. Each replica has its own KV-cache pool (data-parallel trade-off, not a defect). LMCache MP mode when `desired_count > 1`. Mooncake defer until 10+ replica.
- **KV-cache bounds** (corrected):
  - `vocab_size` is INDEPENDENT of KV-cache — only affects embedding/lm_head table (fixed small), does NOT bound cache.
  - **Within 1 sequence**: bounded by `max_model_len` (architectural cap — Qwen3.5: 262,144 native → extended 1,010,000). This is the real within-seq cap.
  - **Across concurrent sequences**: NO natural cap. With infinite VRAM, accepts infinite concurrent requests — bounded only by `--max-num-seqs` (you set) or actual demand.
  - **Real finite VRAM**: `gpu_cache_usage_perc` hits 100% because the pool is a fixed-size allocation at startup from `--gpu-memory-utilization` — this is a cap YOU set, not the model's natural cap. Autoscale on `gpu_cache_usage_perc` measures % of YOUR allocated pool filled.

## 2. TTS engine

**Runtime**: vLLM-Omni v0.22.0 serve (single TTS server).

**Integration**: VieNeu-TTS-v2 integrated into fork `justHman/vllm-omni@feat/vieneu-tts-v0.22` (branched from upstream tag `v0.22.0`). Adds:
- `vllm_omni/transformers_utils/configs/vieneu.py`
- `vllm_omni/model_executor/stage_input_processors/vieneu.py`
- `vllm_omni/model_executor/models/vieneu/pipeline.yaml` (talker stage 0 + codec stage 1, crossfade streaming `codec_chunk_frames=25`, `codec_left_context_frames=25`, TTFB ~0.5s, no boundary clicks)

Launch (verified on Colab T4 cu13):
```bash
vllm serve pnnbao-ump/VieNeu-TTS-v2 \
  --omni --port 8002 --dtype half --trust-remote-code
```

**Presets** (selectable via `POST /api/v1/engines/tts`):

| Priority | id | Model | Params | Weight | VN | Notes |
|---|---|---|---|---:|:---:|---|
| 1 (default) | vieneu-v2-omni | `pnnbao-ump/VieNeu-TTS-v2` | 294M | ~0.5-1GB | ✅ native | Already integrated, streaming |
| 2 | gwen-tts-omni | `g-group-ai-lab/gwen-tts-0.6B` | 0.9B | ~0.6-1GB | ✅ finetune | Streaming |
| 3 | voxcpm2-omni | `openbmb/VoxCPM2` | 0.6B | ~1.2GB | ✅ 600+ langs | RTF 0.025 |

**Runtime swap**: API calls `POST /api/v1/engines/tts` → backend restarts Omni container with new `--model` (hot-swap not supported). Frontend just calls endpoint, backend abstracts.

**Voice clone**: requires `ref_audio_url` + `ref_text` (transcript exact match) + `language` + `sample_rate`. Per-avatar (see §3).

## 3. Avatar engine — half/full-body ONLY (drop head-only/lip-sync)

Target = **half-body + full-body ONLY**. Head-only/lip-sync models (MuseTalk, Ditto, AsymTalker) dropped per user decision — not enough for livestream commerce (need to see host + hands + body holding product).

**Phase F benchmark candidates** (test on T4 16GB + L4 24GB, license test-before-ask):

| Model | Scope | Code | Weights | Realtime | VRAM | License |
|---|---|---|---|---|---|---|
| **AvatarForcing** (KlingAI/Kuaishou) | half/body | ✅ GitHub KlingAIResearch/AvatarForcing | ✅ HF lycui/AvatarForcing | ~29 FPS reported (UNVERIFIED indep) | ~12-16GB est (Wan2.1-1.3B) | Apache-2.0 |
| **EchoMimicV3-Flash** (AntGroup) | half-body | ✅ | ✅ | ❌ RTF ~12 offline (test if can push realtime) | 12-16GB | Apache-2.0 |
| **EchoAvatar** (RobinWitch) | full-body | ✅ realtime deploy code | ✅ HF robinwitch/EchoAvatar (released 2026-06-05, was "TBD" in stale notes) | ✅ 30 FPS <266ms paper | RTX 4090 24GB | ❓ undeclared (paper CC-BY ≠ weights license; test first, ask license later) |

**Rejected/closed** (verified 2026-07-10): StreamAvatar (no real code/weights, only project page + 3rd-party student sub-ckpt), JoyStreamer (academic-only, no code, "commercial not permitted"), InfiniteTalk (Apache-2.0 but batch not realtime — pre-render only), HunyuanVideo-Avatar (Tencent non-OSI license, geo-restricted, not realtime), LongCat-Video-Avatar-1.5 (MIT but batch minutes — pre-render only), OmniHuman-1 (closed API-only), Hallo-Live/LiveAvatar (cloud, >48GB), head-only set (MuseTalk/Ditto/AsymTalker — dropped per user).

**Pre-render optional** (intro/promo, NOT livestream stream): EchoMimicV3-Flash, InfiniteTalk (Apache-2.0), LongCat-Video-Avatar-1.5 (MIT) — batch high-quality for 5-10s intro clips.

`render_backend` enum (revised):
| Value | Scope | Model | Phase |
|---|---|---|---|
| `mock` | head (PIL, dev) | MockRenderBackend | P1-P3 |
| `self_host_avatarforcing_half` | half/body | AvatarForcing | Phase F benchmark |
| `self_host_echomimic_half_prerender` | half-body offline | EchoMimicV3-Flash | Phase F optional pre-render |
| `self_host_echoavatar_full` | full-body | EchoAvatar | Phase F benchmark (license TBD) |
| `self_host_infinitetalk_prerender` | full-body offline | InfiniteTalk | optional pre-render |
| `self_host_longcat_prerender` | full-body offline | LongCat-Video-Avatar-1.5 | optional pre-render (MIT) |
| `cloud_liveavatar` | full-body | LiveAvatar API | enterprise tier (>48GB) |
| `cloud_other` | — | HeyGen/D-ID | future |

**SyncCache** (ECCV 2026, training-free 4.12× speedup, drop-in DiT accelerator): apply when porting AvatarForcing/EchoAvatar DiT models to fit tighter VRAM — Phase F benchmark with and without SyncCache.

**Avatar = per-user custom, scoped**:
```json
POST /api/v1/avatars
{
  "name": "Lan Shop",
  "scope": "half" | "full",
  "ref_photo_url": "https://...",
  "voice": {
    "ref_audio_url": "https://...",
    "ref_text": "Chào mừng mọi người đến với Lan Shop ạ!",
    "language": "vi",
    "sample_rate": 24000
  }
}
→ pre-render idle loop (75 frames @ 25fps = 3s) for this body
← {avatar_id, status: "ready"}
```

Each avatar = ref_photo + voice + idle loop riêng. Idle loop generated 1× at avatar creation, cached, loaded at session start.

## 4. Internal protocol: HTTP/SSE loopback (NOT gRPC) + Pipecat orchestration + Outlines structured output

### 4.1 HTTP/SSE between services
All model servers on same Docker network / ECS cluster. vLLM + vLLM-Omni serve expose OpenAI-compatible HTTP + SSE built-in — 0 wrapper code.

| Destination | Protocol | Payload |
|---|---|---|
| LLM server (vLLM serve) | HTTP + SSE | `POST /v1/chat/completions {stream:true}` → SSE token deltas |
| TTS server (vLLM-Omni serve) | HTTP + audio stream | `POST /v1/audio/speech {stream:true}` → streamed PCM chunks |
| Avatar server (FastAPI) | HTTP | `POST /avatar/{sid}/start_speak` + `/stop` (avatar-server publishes video to LiveKit directly — Cách B) |

**Why HTTP/SSE not gRPC**: loopback overhead ~1ms negligible vs 300-800ms inference. vLLM/vLLM-Omni have no gRPC built-in → wrapper = maintenance burden on every upgrade. curl-debuggable. Revisit gRPC only when multi-node or Triton.

### 4.2 Pipecat — orchestration layer (replaces StreamOrchestrator in API backend)
**Pipecat** (BSD-2, by Daily.co) = open-source framework for realtime voice/video AI agent pipelines. Adopted **Option A**: Pipecat runs INSIDE API backend as the orchestration layer replacing the hand-written StreamOrchestrator. LLM/TTS/Avatar remain 3 separate servers (HTTP/SSE) — Pipecat only wires them + handles interruption + LiveKit transport.

| Concern | Hand-written | Pipecat |
|---|---|---|
| Interruption (barge-in when viewer speaks over host) | 200-500 lines + race conditions | built-in (Silero VAD + token-cancel + cache clear) |
| LiveKit WebRTC transport | manual `livekit-rtc` publish wiring | built-in LiveKit integration |
| Frame routing + custom avatar frame injection | manual VideoSource manage | frame API + custom processor |
| LLM/TTS service wrappers | openai SDK + httpx direct | Pipecat wrappers (must write 1 custom for vLLM-Omni, ~150 lines) |

**Effort**: ~3-5 days migrate StreamOrchestrator → Pipecat pipeline. Saves ~5-8 days vs hand-coding interruption + LiveKit wiring. Cost $0 (self-host). Lock-in medium (Pipecat idiom) but standard.

### 4.3 Outlines — structured JSON generation (LLM controls avatar action)
**Outlines** (Apache-2.0, integrated into vLLM core via `--guided-decoding-backend outlines`). Forces LLM to emit 100% valid JSON schema via FSM token-level masking. Zero RTF overhead (mask at token step, no recompute).

Enable:
```bash
vllm serve cyankiwi/Qwen3.5-4B-AWQ-4bit \
  --port 8001 \
  --enable-prefix-caching \
  --guided-decoding-backend outlines
```

Schema (LLM decides avatar action deterministically):
```python
from pydantic import BaseModel
from typing import Literal

class Utterance(BaseModel):
    speech: str                                # text host speaks
    action: Literal["wave","smile","point","neutral","angry","happy","nod"]
    product_id: str | None                     # product being discussed
    is_final: bool

resp = await llm_client.chat.completions.create(
    model="cyankiwi/Qwen3.5-4B-AWQ-4bit",
    messages=[...],
    extra_body={"guided_json": Utterance},
)
# 100% parse OK → avatar-server knows action (wave/smile/point) without fragile parsing
```

**Why**: livestream host must "wave/smile/point/angry" at right moments. Without Outlines, LLM emits free-text → JSON parse fail ~5-15% → avatar frozen action. With Outlines, 0% fail, deterministic control. $0, ~1 day effort, from P1. LLM does NOT self-report covered talking points — coverage tracked by Director (see §4.6).

### 4.4 LiveKit media plane
- avatar-server publishes **video track** via `livekit-rtc` Python SDK (Cách B — direct publish, no decode in API).
- API backend (via Pipecat) publishes **audio track** (TTS PCM → Opus).
- LiveKit SFU forwards both tracks to FE browsers (WebRTC SRTP/UDP).
- Signaling: WebSocket (SDP/ICE).
- Avatar models generate video only, NOT audio → audio from TTS separately. 2 tracks, LiveKit syncs via RTP timestamp.

### 4.5 External API (FE/BE team SE ↔ API)
- REST `/api/v1/sessions/*` — commands (start/attach/stop/say).
- `POST /api/v1/sessions/{id}/plan/create` — run-plan generation (see §4.6).
- WS `/api/v1/ws/platform/{sid}` — platform adapter → API ingress (comment realtime).
- WS `/api/v1/ws/control/{sid}` — API → BE team SE event stream (director state, decisions, avatar state, warnings). BE forwards to FE as they choose; API does NOT push directly to FE.

### 4.6 Run plan layer — proactive + reactive Director (livestream cadence)

The host is driven by **2 layers working together**: a **run plan** (proactive skeleton) + the **realtime chat loop** (reactive). Output of plan generation is a **structured plan, NOT verbatim script** — verbatim would kill reactivity (the whole point of AI livestream vs pre-recorded video).

**Plan shape** (Outlines `RunPlan` schema, generated 1× before/at Go Live):
```jsonc
{
  "phases": ["opening", "selling(product₁)", "selling(product₂)", ..., "closing"],
  "opening":   { "intro_shop": "...", "persona": "...", "pull_view": "...", "call_to_share": "..." },
  "selling":   { "<product_id>": { "key_selling_points": ["...", "..."], "min_duration": 120, "max_duration": 480, "transition_cue": "..." } },
  "closing":   { "thanks": "...", "follow_cta": "...", "teaser_next": "..." }
  // anticipated_faqs per product — P2
}
```

**Cursor** = Director state marking "where the host is now": `cursor = {phase, product_idx, talking_point_idx}`. A **talking point** = one selling idea from `product.key_selling_points[]` → one utterance ~5-15s.

**Per-tick decision (300ms)** — reactive takes priority, proactive fills silence:
1. drain chat window → cluster → score (runs every tick, even mid-pitch)
2. **if** high-score cluster exists → reactive: answer cluster, cursor does NOT advance (no mid-sentence cut)
3. **elif** talking_point_idx < len-1 → proactive: say next talking point, advance cursor
4. **elif** at last talking point (end of product phase) → if coverage 100% AND Q&A exhaust → transition to next product (cursor: `phase=selling, idx+1, tp=0`); else stay for Q&A
5. **else** idle (wait)

**Platform data never blocks**: chat drains + clusters every tick regardless of what host is saying. High-score clusters interrupt only at talking-point boundaries (not mid-utterance), via the existing `SessionLockRegistry` arbitration (`may_interrupt=True AND new_score > current_score`). Pipeline keeps pushing video chunks to the queue; idle loop covers gaps. This satisfies "while pitching a product, platform data flows into LLM→TTS→Avatar→chunk queue".

**Cold-start solved**: session start → cursor at `phase=opening` → auto-fires first utterance (chào + intro shop + persona + pull view + call to share) without waiting for chat. Each product also pitches its own intro talking points before Q&A — per user requirement.

**Product switching policy (hybrid, env-tunable)**: `PRODUCT_MIN_SEC=120`, `PRODUCT_MAX_SEC=480`, `QA_EXHAUST_K_TICKS=3`, `PURCHASE_SPIKE_THRESHOLD=2`. Early-exit when coverage 100% AND no new high cluster for K ticks AND Q&A exhaustion (repeated question); extend on purchase spike / viral question. Pure time-box is too rigid; pure engagement can trap one product forever; hybrid is the real-host model.

**Idle-vacuum handling**: when no chat for `REINTRO_IDLE_TICKS=60` (18s) consecutive ticks → switch from (a) continue remaining plan points to (b) re-intro lite (re-greet + pull view + tease next product). Default (a); (b) triggers on prolonged silence — hybrid mimics a real host.

**Coverage tracking — Director semantic match (chosen over LLM self-report)**: after each utterance, Director embeds `utterance.speech` via the existing `BiEncoderEmbedder` (bkai vietnamese-bi-encoder) and cosine-matches against `product.key_selling_points[]` (threshold `COVERAGE_MATCH_THRESHOLD=0.75` env) → marks `covered_points[product_id] ∪ {matched}`.
- **Why Director, not LLM**: coverage drives the financial decision to switch product (stopping the current sell). Qwen3.5-4B self-report risks hallucination ("covered" when vague) → premature switch → lost revenue. Director match is deterministic, auditable (log match scores), ~1ms cost (negligible vs 300-800ms LLM). Tight cosine match errs safe (host over-talks > under-sells). The Utterance schema stays lean: `{speech, action, product_id, is_final}` — no coverage field.
- **P2 enhancement (optional)**: add LLM field `addressed_point_hint: str | None` as a recall boost; Director match still wins on disagreement.

**MVP scope**: run plan ON from P1 (intro phase is needed — without it the demo breaks). Anticipated FAQs field deferred to P2. Reactive-first path (chat→cluster→LLM→TTS→Avatar) ships first; proactive cursor layer added on top once the realtime path is stable.

## 5. Deployment: 3 GPU/CPU instances + 4 support services (ECS, not Compose)

**MVP = prod = AWS ECS** (no Colab, no Docker Compose for runtime). Control plane free. GPU via EC2 launch type. K8s deferred.

### 5.1 Instance allocation (Seoul, verified AWS Pricing API 2026-07-10)

| # | Role | Instance | GPU | VRAM | On-demand/mo | Spot/mo | Capacity Provider |
|---|---|---|---|---|---:|---:|---|
| 1 | LLM + TTS (colocate, share 1 GPU) | `g6.xlarge` | L4 | 24GB | $722 | $206 | EC2 Spot ASG → On-Demand before prod |
| 2 | Avatar | `g4dn.xlarge` | T4 | 16GB | $472 | $179 | EC2 Spot ASG → On-Demand before prod |
| 3 | Backend API (CPU only) | Fargate Spot ARM64 (Graviton `c7g`) | — | — | $36 | $11 | Fargate Spot |

Support services (no GPU, CPU-only):
| Service | MVP container | Prod managed | Spot/mo | On-Demand/mo | Env toggle |
|---|---|---|---:|---:|---|
| LiveKit Server (SFU, self-host Go) | Fargate Spot | Fargate (self-host OK) | $22 | $72 | always on |
| PostgreSQL (DB runtime) | **RDS** db.t4g.medium + gp3 100GB | RDS (auto-backup, multi-AZ upgrade, patch) | $86 | $86 | always on |
| Redis (ChatQueue, locks, rate-limit) | **ElastiCache** cache.t4g.small | ElastiCache (failover auto) | $12 | $12 | always on |
| **LMCache server** (cross-replica KV-cache, MP mode) | **EC2 c7g.2xlarge Spot ASG** (stateful, not Fargate) | c7g.4xlarge on-demand when prod | $95 | $238 | **`LMCACHE_ENABLED=true/false`** |
| ALB + LCU + S3 + CloudWatch + DataTransfer | — | — | $56 | $56 | always on |
| Cloudflare Free (DNS + edge WAF/DDoS) | — | — | $0 | $0 | always on |
| Docker Hub public images (code+deps) | — | — | $0 | $0 | always on |

> Network: **public-subnet only, 2 public AZs** for ALB/RDS (workload single-AZ pin; no NAT/private). Edge: **Cloudflare Free**. Registry: **Docker Hub public** + weights on S3. Full: `aws-architecture.md`.

**Total prod (Seoul, Spot MVP, LMCACHE_ENABLED=true test)**: ~$662/mo. **On-demand**: ~$1,650/mo. `LMCACHE_ENABLED=false` → ~$571 Spot / ~$1,412 On-Demand. Full: `aws-architecture.md` §8.

### 5.2 GPU sharing between LLM + TTS (1 GPU, 2 containers, 1 Task)

ECS technique (spec-03 §2.2):
- Only LLM container declares `resourceRequirements: {type: GPU, value: 1}`.
- TTS container does NOT declare GPU resource — uses `NVIDIA_VISIBLE_DEVICES` env var pointing to same GPU UUID.
- `--gpu-memory-utilization` NOT split evenly: **LLM ~0.6 / TTS ~0.25** (LLM KV-cache heavier than TTS), ~0.15 buffer. Verify via VRAM footprint of VieNeu-TTS-v2 (backlog).

### 5.3 ECS structure

| Layer | Count | Content |
|---|---:|---|
| Cluster | 1 | whole infra |
| Capacity Provider | 2 | (a) EC2 Spot ASG GPU pool (mix g6+g4dn) + c7g for LMCache; (b) Fargate Spot for Backend + LiveKit |
| Task Definition | 4 | Backend (1 container) / **LLM+TTS (2 containers, 1 Task, share GPU)** / Avatar (1 container) / **LMCache-server (1 container, CPU only, EC2 launch)** |
| Service | 4 | 1 Service per Task, `desired_count=1` MVP (LMCache `desired_count=0` when `LMCACHE_ENABLED=false`) |

### 5.4 AMI
- GPU instances: **AWS Deep Learning AMI** (Ubuntu, NVIDIA driver + CUDA pre-installed).
- LMCache (EC2 c7g): Amazon Linux 2023 ARM64 (Graviton, free).
- Backend/LiveKit (Fargate): Fargate-managed ARM64 runtime (no AMI).
- Avoid Windows/RHEL/SUSE (license fees).

### 5.5 LMCache MP mode — cross-replica KV-cache sharing (env-togglable)

**2 prefix-cache layers (independent):**
- **Layer 1 — `--enable-prefix-caching`** (vLLM built-in, 0 cost, 1 replica): ALWAYS ON. System prompt + persona + product catalog prefix cached, repeated across every viewer message.
- **Layer 2 — LMCache MP mode** (cross-replica, env-togglable via `LMCACHE_ENABLED`): ON for scale test, OFF after to save cost. Only valuable when `llm-tts` Service `desired_count > 1` (2+ replicas share cache).

**LMCache MP mode architecture** (official recommendation, NOT in-process):
```text
Service: lmcache-server (4th Service, Fargate c7g.2xlarge Spot test / c7g.4xlarge prod)
  └─ Task: 1 container CPU-only (no GPU)
       lmcache server --host 0.0.0.0 --port 5555 \
         --l1-size-gb 8 --eviction-policy LRU --chunk-size 256
       port 5555 (ZMQ, vLLM connect) + port 8080 (HTTP metrics)
       desired_count=1 (single, NO scale — shared by all LLM replicas)

Service: llm-tts (N replicas when autoscale)
  └─ each vLLM container:
       PYTHONHASHSEED=0  # MANDATORY — else hash key mismatch → always miss
       LMCACHE_CONFIG_FILE=/app/lmcache_config.yaml
       vllm serve <model> \
         --enable-prefix-caching \           # Layer 1 always on
         --gpu-memory-utilization 0.6 \
         --kv-transfer-config '{"kv_connector":"LMCacheMPConnector","kv_role":"kv_both"}'

lmcache_config.yaml (baked into LLM image):
  chunk_size: 256
  local_cpu: true
  remote_url: "lm://lmcache-server.internal:5555"  # ECS Service Connect DNS
  remote_serde: "cachegen"
```

**⚠️ PYTHONHASHSEED=0 mandatory**: if not set, cache hash keys differ between replicas → 100% miss. Must pin fixed.

**⚠️ Instance RAM**: `--l1-size-gb 8` needs >8GB RAM (cache + OS + overhead). `c7g.large` (4GB) insufficient. **c7g.2xlarge (16GB RAM, ~$30/mo Spot)** for test with L1=8GB; **c7g.4xlarge (32GB, ~$195/mo on-demand)** for prod with L1=16GB. Compromise: L1=8GB enough for persona+product prefix (MVP); bump if cache-hit ratio low.

**Env toggle**:
- `LMCACHE_ENABLED=true` (test phase): lmcache-server `desired_count=1`, LLM uses LMCacheMPConnector.
- `LMCACHE_ENABLED=false` (post-test cost-save): lmcache-server `desired_count=0` ($0), LLM drops `--kv-transfer-config`, keeps only `--enable-prefix-caching` (Layer 1, 0 cost).

**Mooncake** (P2P KV transfer) defer until 10+ replica.

## 6. Region: Seoul (ap-northeast-2) — VERIFIED, both MVP and prod

Verified via AWS Pricing API 2026-07-10 (NOT from spec-02 which was wrong about Malaysia):

| Region | g6.xlarge | g4dn.xlarge | g5.xlarge | g6.12xlarge | Distance VN |
|---|---|---|---|---|---|
| **ap-northeast-2 (Seoul)** ✅ | $0.9896/hr = $722/mo | $0.6470/hr = $472/mo | — | — | medium |
| ap-southeast-5 (Malaysia) ❌ | $1.0138/hr = $740/mo | **NOT AVAILABLE** (count 0) | NOT AVAILABLE | $5.7966/hr | nearest but no T4 |
| ap-south-1 (Mumbai) | $0.9664/hr = $705/mo | $0.579/hr = $422/mo | — | — | farthest |
| ap-southeast-1 (Singapore) ❌ | NOT AVAILABLE | NOT AVAILABLE | — | — | near |

**Why Seoul (I chose for you — your money = my money)**:
1. **Cheaper than Malaysia by $286/mo (23%)** — Malaysia lacks g4dn → Avatar must use L4 ($740 vs $472).
2. **Quota already requested** — Seoul 2 cases opened (G/VT 8 vCPU case 178329128100829, P 96 vCPU case 178329128900161). Switching region = wasted effort.
3. **Has both g6 + g4dn** — Malaysia only g6.
4. **Latency acceptable** — Seoul-VN ~60-80ms RTT, livestream has buffer, not gaming-grade sub-50ms needed.
5. Mumbai cheaper $67/mo but farther + no quota requested → not worth migrating (effort > saving).

Quota cases (Seoul, already submitted):
- Running On-Demand G and VT instances: 8 vCPUs (case 178329128100829)
- Running On-Demand P instances: 96 vCPUs (case 178329128900161)

## 7. Autoscaling (spec-03 §3) — metrics, NOT %VRAM

Two tiers:
| Tier | Attached to | Scales |
|---|---|---|
| Service Auto Scaling | Service | Task count |
| Capacity Provider Managed Scaling | Capacity Provider | EC2 instance count |

⚠️ Scaling "LLM+TTS" Service always doubles BOTH models (same Task Definition).

**Trigger metrics (NOT default %VRAM)**:
- `vllm:num_requests_waiting` — scale-out when > 5-10 sustained 30-60s (tune per real traffic).
- `vllm:gpu_cache_usage_perc` — scale-out when sustained > 70-80%. This is % of KV-cache pool filled (bounded 0-100%, NOT infinite, NOT %VRAM allocation). `--gpu-memory-utilization` is STATIC at startup, does not reflect load → must NOT use as autoscale metric.
- **Mandatory `MaxCapacity`** on Service Auto Scaling + `MaxSize` on Capacity Provider ASG — prevents infinite autoscale. Plus CloudWatch Billing Alarm as second layer.

**Why not %VRAM**: `--gpu-memory-utilization` is set once at startup (static), does not reflect runtime load. `gpu_cache_usage_perc` measures actual KV-cache pool fill at runtime → reflects load.

## 8. ARM (Graviton)

- **Backend API**: ✅ Graviton (`c7g`/`c8g`) via Fargate Spot ARM64 — saves 20-40% vs x86, Python/FastAPI builds ARM64 easily, low compat risk.
- **GPU workload (LLM/TTS/Avatar)**: ❌ NOT ARM. Only G5g (Graviton2 + T4G, 2021, old). PyTorch/CUDA ARM64 wheels immature, diffusers-heavy Avatar pipeline risky. Stay x86.

## 9. Databases — 2 separate, per-service (NOT shared)

| DB | Owner | Stores | Endpoints |
|---|---|---|---|
| **DB business** | BE team SE | users, shops, products, orders, payments, staff | `/user/*`, `/shop/*` (team SE codes these) |
| **DB runtime** | You (API server) | sessions, session_products (snapshot frozen at attach), viewer_msgs, director_decisions, llm_call_log, tts_call_log, audit | `/sessions/*`, `/admin/*` (you code) |

**No DB sharing cross-team** (anti-pattern: coupling, security boundary, runtime dependency, replay mismatch). API server receives `products[]` via `/attach` push-based, stores snapshot (frozen). Shop edits DB business mid-livestream → snapshot unchanged → host says correct price, replay shows correct history.

**DB runtime schema** (Postgres 16 + pgvector + pg_trgm + LISTEN/NOTIFY):
- `sessions(id, shop_id, status, render_backend, avatar_id, created_at, ended_at)`
- `session_products(session_id, product_id, snapshot JSONB, created_at)` — frozen at attach
- `viewer_msgs(session_id, viewer_id, text, ts)`
- `director_decisions(session_id, action, product_id, score, text, ts)`
- `llm_call_log`, `tts_call_log` — audit
- pgvector: product embeddings (BiEncoder) for Director.scorer.retrieve_product
- LISTEN/NOTIFY: `pg_notify('director_cycle:{sid}', json)` → API → WS fanout

**Redis 7**: ChatQueue (`XADD chat:{sid}`), session locks, rate-limit counters, LMCache backend (P4).

## 10. API endpoints — unified `/api/v1/` root

REST = default (no `rest/` prefix). WS + media have prefix (different protocol, different infra route). No more `/ws/*` `/mock/*` lone patterns.

| Group | Endpoint | Protocol | Who calls (prod) | Who calls (your test) |
|---|---|---|---|---|
| Sessions | `POST /api/v1/sessions` | REST | BE team SE | FE test (skip /user/login) |
| | `POST /api/v1/sessions/{id}/attach` | REST | BE team SE | FE test |
| | `POST /api/v1/sessions/{id}/stop` | REST | BE team SE | FE test |
| | `POST /api/v1/sessions/{id}/say` | REST | BE team SE | FE test |
| | `POST /api/v1/sessions/{id}/plan/create` | REST | BE team SE | FE test |
| Avatars | `POST /api/v1/avatars` | REST | BE team SE | FE test |
| | `GET /api/v1/avatars` | REST | BE team SE | FE test |
| | `PUT/DELETE /api/v1/avatars/{id}` | REST | BE team SE | FE test |
| | `POST /api/v1/avatars/{id}/idle/regenerate` | REST | BE team SE | FE test |
| Realtime WS | `WS /api/v1/ws/platform/{sid}` | WebSocket | BE team SE (platform adapter) | FE test (mock chat panel) |
| | `WS /api/v1/ws/control/{sid}` | WebSocket | BE team SE (event consumer, forwards to FE) | FE test (consumer role) |
| Media | `POST /api/v1/media/livekit/room/{sid}` | REST → LiveKit | BE team SE gets token, FE joins LiveKit | FE test joins LiveKit |
| | `GET /api/v1/media/frame/{sid}.png` | HTTP | debug snapshot | debug |
| Engines | `POST /api/v1/engines/tts` | REST | BE team SE | FE test |
| | `POST /api/v1/engines/llm` | REST | BE team SE | FE test |
| | `GET /api/v1/engines` | REST | BE team SE | FE test |
| Health | `GET /api/v1/health/ready` | REST | BE team SE + monitor | FE test |
| | `GET /api/v1/health/live` | REST | monitor | — |
| Admin | `/api/v1/admin/*` | REST | You (ops) | You |

`/user/*`, `/shop/*` = BE team SE codes + DB business. You do NOT code these.

## 11. LiveKit WebRTC from P1 (Cách B, unified mock + real)

No MJPEG. LiveKit from day 1. Idle loop = pre-rendered frames pushed into LiveKit VideoSource (not MJPEG endpoint).

- **avatar-server container** has LiveKit SDK + mock/MuseTalk/EchoMimic backend. Publishes video track directly (Cách B). Swap mock→MuseTalk = swap backend class, no flow change.
- **API backend** publishes audio track (TTS PCM → Opus via livekit-rtc AudioSource).
- 2 publishers in room, LiveKit SFU merges for subscriber.
- FE team SE: `livekit-client` JS SDK subscribe both tracks, render `<video>` + `<audio>`.
- **Idle loop**: 75 frames pre-rendered at avatar creation, publish loop 40ms/tick (25fps) — utterance frame if queue has one, else idle frame (`idle_idx % 75`). Background task from `/attach` to `/stop`. Prevents black/freeze when host silent.

## 12. Files affected

- `core/config.py` — defaults: `LLM_ENGINE=vllm`, `LLM_MODEL=cyankiwi/Qwen3.5-4B-AWQ-4bit`, `TTS_ENGINE=vllm-omni`, `TTS_MODEL=pnnbao-ump/VieNeu-TTS-v2`, `RENDER_BACKEND=mock`, region/instance flags.
- `core/engine_manager.py` — remove llamacpp presets; add `vllm-remote` LLM preset + `vllm-omni` TTS presets (remote gRPC/HTTP endpoints, no in-process load).
- `core/llm/adapters/llamacpp.py` — keep as deprecated stub (same as sglang).
- `core/tts/adapters/vieneu.py` — keep native adapter as offline fallback.
- `core/store/postgres.py`, `core/store/redis.py`, `core/store/vector.py` — NEW runtime data layer.
- `core/api/v1.py` — rename `/lite/*` → `/sessions/*`, `/ws/*` → `/api/v1/ws/*`, `/mock/*` → `/api/v1/media/*`, add `/avatars/*`, `/engines/llm`, `/admin/*`.
- `services/llm/Dockerfile`, `services/tts/Dockerfile` (vllm-omni fork + `[vieneu]` extra), `services/avatar/Dockerfile` (FastAPI + LiveKit SDK + mock/MuseTalk/EchoMimic), `services/backend/Dockerfile` (ARM64 Graviton).
- `ecs/cluster.tf`, `ecs/capacity-providers.tf`, `ecs/task-definitions.tf`, `ecs/services.tf` — Terraform for ECS.
- `docker-compose.yml` — for local dev only (Colab gone).
- `architecture.md` — update model table, 3-instance diagram, LiveKit flow.

## 13. Open risks / verification backlog (from spec-04 + user decisions)

1. Verify `cyankiwi/Qwen3.5-4B-AWQ-4bit` loads in vLLM base text-only (`--language-model-only`). — task #48
2. Build + test vLLM-Omni Docker image from fork on L4 24GB (Seoul g6) — currently verified only Colab T4 cu13.
3. Pre-bake weights vs mount/cache runtime (image size vs startup).
4. GPU memory budget per service (LLM 0.6 / TTS 0.25 / buffer 0.15) — measure VieNeu footprint.
5. **Benchmark 3 half/full-body avatar models on T4 16GB + L4 24GB** (drop head-only MuseTalk/Ditto/AsymTalker):
   - AvatarForcing (Apache-2.0, ~29 FPS reported, UNVERIFIED independent) — task #51
   - EchoMimicV3-Flash (Apache-2.0, RTF 12 offline — test if pushable to realtime)
   - EchoAvatar (full-body, weights released 2026-06-05, license TBD — test first, ask license later) — task #52
6. Benchmark AWQ INT4 vs INT8-INT4 before locking prod. — task #48
7. Implement `NVIDIA_VISIBLE_DEVICES` GPU sharing for LLM+TTS pair.
8. Build ECS Cluster + 2 Capacity Provider (Seoul) + 4 Task Definition + 4 Service (Backend / LLM+TTS / Avatar / LMCache-server).
9. Configure Service Auto Scaling on `num_requests_waiting` + `gpu_cache_usage_perc`, with `MaxCapacity`/`MaxSize` + Billing Alarm.
10. Test build Backend Docker image ARM64 on Graviton (`c7g`).
11. **Before real broadcast (paying customer)**: switch GPU from Spot to On-Demand or mixed base/weight.
12. LMCache MP mode integration (env-togglable `LMCACHE_ENABLED`, PYTHONHASHSEED=0, lmcache-server 4th Service). — task #47
13. Pipecat migration: replace StreamOrchestrator with Pipecat pipeline in API backend, write custom vLLM-Omni TTS service wrapper (~150 lines). — task #54
14. Outlines enable: `--guided-decoding-backend outlines` + Utterance schema (speech/action/product_id/is_final). — task #55
15. Update stale project notes: `notes/2026-06-22-avatar-strategy-decision.md` + `notes/2026-07-03-weekly-digest.md` — EchoAvatar weights RELEASED (was "TBD"). — task #53
16. EchoAvatar license verify with author (RobinWitch GitHub issue/email) — weights license undeclared, paper CC-BY ≠ weights license. Test first, ask license in parallel.
17. SyncCache drop-in accelerator (ECCV 2026, 4.12× speedup) — apply when benchmarking AvatarForcing/EchoAvatar DiT models to fit tighter VRAM.
18. Verify `--enable-prefix-caching` Layer 1 works as expected on 1 replica (system prompt + persona + product catalog prefix hit ratio).

## 14. Decision log

- LLM: vLLM 0.22.0 + `cyankiwi/Qwen3.5-4B-AWQ-4bit` (INT4 thuần). INT8-INT4 benchmark before prod.
- LLM prefix caching: 2 layers — `--enable-prefix-caching` (built-in, always on, 0 cost) + LMCache MP mode (env-togglable `LMCACHE_ENABLED`, cross-replica, P4 scale).
- KV-cache bounds: within-seq = `max_model_len` 262,144 (NOT vocab); cross-seq = `--max-num-seqs` + VRAM pool you allocate (NOT natural cap). `gpu_cache_usage_perc` = % of YOUR allocated pool.
- TTS: vLLM-Omni serve, default `pnnbao-ump/VieNeu-TTS-v2`, alternatives gwen-tts-0.6B + VoxCPM2. Voice needs ref_audio + ref_text + language + sample_rate.
- Avatar: half/full-body ONLY (drop head-only MuseTalk/Ditto/AsymTalker). Phase F benchmark 3: AvatarForcing (Apache-2.0) + EchoMimicV3-Flash (Apache-2.0, RTF 12 offline) + EchoAvatar (full-body, weights released, license TBD — test first ask later). Pre-render optional: InfiniteTalk/LongCat (batch). Rejected: StreamAvatar/JoyStreamer/HunyuanVideo (no code or license risk).
- Orchestration: Pipecat (BSD-2, Option A) replaces StreamOrchestrator in API backend. LiveKit transport + interruption built-in. Custom vLLM-Omni TTS wrapper ~150 lines.
- Structured output: Outlines (`--guided-decoding-backend outlines`) from P1. Utterance schema → avatar action deterministic, 0% parse fail.
- Internal: HTTP/SSE loopback (NOT gRPC). vLLM/vLLM-Omni built-in OpenAI-compatible.
- Media: LiveKit WebRTC from P1 (Cách B avatar-server publish directly, API via Pipecat publish audio). No MJPEG.
- Region: Seoul (ap-northeast-2) both MVP + prod. Verified AWS Pricing API 2026-07-10. Malaysia lacks g4dn → Avatar would need L4 ($286/mo more).
- Orchestration platform: ECS (free control plane, GPU via EC2 launch type). No K8s, no Colab, no Docker Compose for runtime.
- Instances: LLM+TTS g6.xlarge (colocate, GPU share 0.6/0.25), Avatar g4dn.xlarge, Backend Fargate ARM. Support: LiveKit Fargate + Postgres RDS + Redis ElastiCache + **LMCache-server Fargate c7g.2xlarge Spot (env-togglable)**.
- GPU sharing: NVIDIA_VISIBLE_DEVICES + 1 container declares GPU resource.
- Autoscaling: `num_requests_waiting` + `gpu_cache_usage_perc` (NOT %VRAM), MaxCapacity + Billing Alarm.
- ARM: Backend Graviton Fargate Spot. NOT ARM for GPU. LMCache-server also Graviton (CPU+RAM only).
- AMI: DL AMI for GPU, Amazon Linux 2023 ARM for backend/LMCache.
- DB: 2 separate (business team SE, runtime you). No cross-team share. Postgres+pgvector+Redis.
- Endpoints: unified `/api/v1/` root. REST default, WS + media prefixed. `/ws/control` = event stream API→BE (NOT direct FE).
- Scale features env-togglable: `LMCACHE_ENABLED` (test on, cost-save off after). Same pattern for future scale features (Redis Streams multi-instance, OpenTelemetry, etc.).
- No llama.cpp/GGUF anywhere. No MJPEG anywhere. No head-only avatar (MuseTalk/Ditto/AsymTalker dropped).