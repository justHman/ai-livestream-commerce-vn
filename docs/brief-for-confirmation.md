# Brief for Confirmation â€” VN Live-Commerce Host Architecture (v2.0)

> Status: **CONFIRMED 2026-07-10** (product/engines) + AWS ops confirmed 2026-07-11.
> Supersedes v1.x. Companions: `scope-engine-and-models.md`, `aws-architecture.md`, `terraform-layout.md`, `cicd-branch-strategy.md`, `aws-pricing-seoul.csv`.
> Active plans: `../plans/00-implement-aws-stack.md` (infra/Docker/CI), `../plans/01-app-feature-backlog.md` (Pipecat/Outlines/run-plan/avatar).

## 1. Goal

Move from monolithic backend (LLM/TTS in-process) to **3-instance + 3-support-service architecture** on AWS ECS (Seoul), production-grade from day 1. No Colab, no Docker Compose for runtime (Compose only for local dev).

## 2. Confirmed decisions

### A. LLM
- **Engine**: vLLM 0.22.0 (stable) base. NOT vLLM-Omni. NOT llama.cpp.
- **Model**: `cyankiwi/Qwen3.5-4B-AWQ-4bit` (AWQ INT4 thuáº§n, Apache-2.0, Marlin kernel optimized for vLLM 0.22.0).
- **INT8-INT4 alt**: `cyankiwi/Qwen3.5-4B-AWQ-INT8-INT4` exists â€” benchmark vs INT4 before locking prod (task #48). Switch only if INT4 quality insufficient AND INT8-INT4 passes vLLM compat.
- **Prefix caching â€” 2 layers (independent)**:
  - **Layer 1 (P1, ALWAYS ON, 0 cost)**: `--enable-prefix-caching` built-in flag. Works within 1 replica. Classic use case: long system prompt (persona host + product catalog) repeated identically across every viewer message â†’ prefix cached, first-token latency drops. Zero effort, zero cost.
  - **Layer 2 (P4 scale, env-togglable `LMCACHE_ENABLED`)**: LMCache MP mode for cross-replica KV-cache sharing. See Â§E below.
- **Scaling**: Data Parallelism (replica) via vLLM continuous batching. Each replica has own KV-cache pool. LMCache MP mode when `desired_count > 1`. Mooncake defer 10+ replica.
- **KV-cache bounds (corrected)**:
  - `vocab_size` INDEPENDENT of KV-cache (only affects embedding/lm_head table, fixed small).
  - Within 1 sequence: bounded by `max_model_len` (Qwen3.5: 262,144 native â†’ extended 1,010,000). Real within-seq cap.
  - Across concurrent sequences: NO natural cap. Infinite VRAM = infinite concurrent requests, bounded only by `--max-num-seqs` (you set) or demand.
  - Real finite VRAM: `gpu_cache_usage_perc` hits 100% because pool is fixed-size allocation at startup from `--gpu-memory-utilization` â€” cap YOU set, not model's natural cap. Autoscale on this metric.
- **Outlines structured output (P1)**: `--guided-decoding-backend outlines` + Utterance schema `{speech, action, product_id, is_final}` â†’ 100% valid JSON, avatar action deterministic. See Â§D.
- **Run plan generation (P1, ON by default)**: `POST /api/v1/sessions/{id}/plan/create` (see Â§M) â€” generates a **structured run plan** (NOT verbatim script), pre-live or at session start. Output = phases Ă— products Ă— talking points Ă— anticipated FAQ (deferred to P2), via Outlines `RunPlan` schema. Used as the proactive driver for the Director (see Â§D run-plan layer). Reuses same vLLM server + `--guided-decoding-backend outlines`; bounded output, no streaming. If not called, Director auto-generates a minimal plan from `products[]` + persona defaults. Shop-user may review/edit before Go Live (brand safety).
- llama.cpp/GGUF: removed from active use, file kept as deprecated stub.

### B. TTS
- **Engine**: vLLM-Omni v0.22.0 serve.
- **Fork**: `justHman/vllm-omni@feat/vieneu-tts-v0.22` (branched upstream v0.22.0). VieNeu-TTS-v2 integrated (verified Colab T4 cu13).
- **Default**: `pnnbao-ump/VieNeu-TTS-v2` (streaming, crossfade `codec_chunk_frames=25`, TTFB ~0.5s).
- **Alts**: `g-group-ai-lab/gwen-tts-0.6B`, `openbmb/VoxCPM2` (selectable via `/engines/tts`).
- **Voice clone**: requires `ref_audio_url` + `ref_text` + `language` + `sample_rate` (per-avatar).

### C. Avatar â€” half/full-body ONLY (drop head-only/lip-sync per user decision)
- **Dropped**: MuseTalk, Ditto, AsymTalker (head-only/lip-sync â€” not enough for livestream commerce, need host + hands + body holding product).
- **Phase F benchmark 3 candidates** (test on T4 16GB + L4 24GB, license test-first-ask-later per user):
  - **AvatarForcing** (KlingAI/Kuaishou, Apache-2.0, 1.3B Wan2.1, ~29 FPS reported UNVERIFIED indep) â€” task #51. Code: github.com/KlingAIResearch/AvatarForcing. Weights: HF lycui/AvatarForcing. Commercial-clear license.
  - **EchoMimicV3-Flash** (AntGroup, Apache-2.0, RTF ~12 offline) â€” test if pushable to realtime; if not, pre-render only.
  - **EchoAvatar** (RobinWitch, full-body, weights RELEASED 2026-06-05 on HF robinwitch/EchoAvatar, verified 2026-07-10 task #53 done) â€” 30 FPS <266ms paper, RTX 4090 24GB. License UNDECLARED (paper CC-BY â‰  weights license) â†’ test first, ask author in parallel (task #52).
- **Rejected/closed** (verified 2026-07-10): StreamAvatar (no real code/weights), JoyStreamer (academic-only), InfiniteTalk/LongCat (batch not realtime â€” pre-render only), HunyuanVideo-Avatar (Tencent non-OSI license, geo-restricted), OmniHuman-1 (closed).
- **Pre-render optional** (intro/promo, NOT livestream): EchoMimicV3-Flash, InfiniteTalk (Apache-2.0), LongCat-Video-Avatar-1.5 (MIT).
- **SyncCache** (ECCV 2026, training-free 4.12Ă— speedup): drop-in DiT accelerator, apply when benchmarking AvatarForcing/EchoAvatar to fit tighter VRAM.
- **`render_backend` enum**: `mock` | `self_host_avatarforcing_half` | `self_host_echomimic_half_prerender` | `self_host_echoavatar_full` | `self_host_infinitetalk_prerender` | `self_host_longcat_prerender` | `cloud_liveavatar` | `cloud_other`.
- **Per-avatar custom + idle loop**: `POST /avatars {scope: half|full, ref_photo_url, voice}` â†’ pre-render idle loop (75 frames @ 25fps = 3s) â†’ cache â†’ load at session start.

### D. Internal protocol â€” HTTP/SSE loopback (NOT gRPC) + Pipecat orchestration + Outlines structured output
- **HTTP/SSE between services**: LLM (`/v1/chat/completions` SSE), TTS (`/v1/audio/speech` audio stream), Avatar (`/avatar/{sid}/start_speak`+`/stop`). vLLM/vLLM-Omni built-in OpenAI-compatible, 0 wrapper. Loopback overhead ~1ms negligible vs 300-800ms inference. curl-debuggable. Revisit gRPC only when multi-node or Triton.
- **Pipecat (BSD-2, Option A)**: replaces hand-written StreamOrchestrator in API backend (task #54). LLM/TTS/Avatar remain 3 separate HTTP/SSE servers â€” Pipecat only wires them + handles interruption (Silero VAD + barge-in + token-cancel + cache clear, saves 200-500 lines) + LiveKit transport built-in (saves manual wiring) + frame API for avatar injection. MUST write 1 custom Pipecat TTS service wrapper for vLLM-Omni (~150 lines). Effort ~3-5 days, $0.
- **Outlines (Apache-2.0, vLLM built-in)**: `--guided-decoding-backend outlines` from P1 (task #55). Forces 100% valid JSON via FSM token-level masking, 0 RTF overhead. Schema `Utterance {speech, action: wave|smile|point|neutral|angry|happy|nod, product_id, is_final}` â†’ avatar-server receives deterministic action, no fragile free-text parsing (without Outlines ~5-15% parse fail). LLM does NOT self-report covered points â€” coverage is tracked by Director (see run-plan layer below).
- **Run plan layer (proactive + reactive Director)** â€” the host is driven by 2 layers working together:
  - **Run plan** = structured, NOT verbatim. Generated by `POST /api/v1/sessions/{id}/plan/create` before/at Go Live. Shape:
    ```
    phases: [opening, selling(productâ‚), selling(productâ‚‚), ..., selling(productâ‚™), closing]
    opening:   {intro_shop, persona, pull_view, call_to_share}
    selling(p): {key_selling_points[], min_duration, max_duration, transition_cue}  + anticipated_faqs[] (P2)
    closing:   {thanks, follow_cta, teaser_next}
    ```
  - **Cursor** = Director state marking "where the host is now": `cursor = {phase, product_idx, talking_point_idx}`. A talking point is one selling idea from `product.key_selling_points[]` (one utterance ~5-15s).
  - **Per-tick Director decision (300ms)** â€” reactive takes priority, proactive fills silence:
    1. drain chat window â†’ cluster â†’ score (always runs, even mid-pitch)
    2. **if** high-score cluster exists â†’ reactive: answer cluster, cursor does NOT advance (no mid-sentence cut)
    3. **elif** talking_point_idx < len-1 â†’ proactive: say next talking point, advance cursor
    4. **elif** at last talking point (end of product phase) â†’ if coverage 100% AND Q&A exhaust â†’ transition to next product (cursor: phase=selling, idx+1, tp=0); else stay for Q&A
    5. **else** idle (wait)
  - **Platform data never blocks**: chat drains + clusters every tick regardless of what host is saying. High-score clusters interrupt only at talking-point boundaries (not mid-utterance). Pipeline keeps pushing video chunks to the queue; idle loop covers gaps. This satisfies "while pitching a product, platform data flows into LLMâ†’TTSâ†’Avatarâ†’chunk queue".
  - **Cold-start solved**: session start â†’ cursor at `phase=opening` â†’ auto-fires first utterance (chĂ o + intro shop + persona + pull view + call to share) without waiting for chat. Each product also pitches its own intro talking points before Q&A â€” per user requirement.
  - **Product switching policy (hybrid, env-tunable)**: `PRODUCT_MIN_SEC=120`, `PRODUCT_MAX_SEC=480`, `QA_EXHAUST_K_TICKS=3`, `PURCHASE_SPIKE_THRESHOLD=2`. Early-exit when coverage 100% AND no new high cluster for K ticks AND Q&A exhaustion (repeated question); extend on purchase spike / viral question.
  - **Idle-vacuum handling**: when no chat for `REINTRO_IDLE_TICKS=60` (18s) consecutive ticks â†’ switch from (a) continue remaining plan points to (b) re-intro lite (re-greet + pull view + tease next product). Default (a); (b) triggers on prolonged silence â€” hybrid mimics a real host.
  - **Coverage tracking â€” Director semantic match (chosen)**: after each utterance, Director embeds `utterance.speech` via the existing `BiEncoderEmbedder` (bkai vietnamese-bi-encoder) and cosine-matches against `product.key_selling_points[]` (threshold `COVERAGE_MATCH_THRESHOLD=0.75` env) â†’ marks `covered_points[product_id] âˆª {matched}`. Rationale: coverage is a financial decision (switching product = stopping current sell); Qwen3.5-4B self-report risks hallucination â†’ premature switch â†’ lost revenue. Director match is deterministic, auditable, ~1ms cost (negligible vs 300-800ms LLM). Tight match errs safe (host over-talks > under-sells). Optional P2 enhancement: LLM `addressed_point_hint` field as a recall boost, Director match still wins on disagreement.
  - **MVP scope**: run plan ON from P1 (intro phase is needed â€” without it the demo breaks). Anticipated FAQs field deferred to P2. Reactive-first path (chatâ†’clusterâ†’LLMâ†’TTSâ†’Avatar) ships first; proactive cursor layer added on top once realtime path is stable.

### E. LiveKit WebRTC from P1 (CĂ¡ch B, unified mock + real) + LMCache MP mode
- **No MJPEG**. LiveKit from day 1.
- **avatar-server** container: LiveKit SDK + mock/AvatarForcing/EchoMimic/EchoAvatar backend â†’ publishes **video track** directly (CĂ¡ch B). Swap backend = swap class.
- **API backend (via Pipecat)**: publishes **audio track** (TTS PCM â†’ Opus).
- Avatar models generate video only, NOT audio â†’ audio from TTS separately. 2 tracks, LiveKit syncs via RTP timestamp.
- **Idle loop**: 75 pre-rendered frames pushed into LiveKit VideoSource, 40ms/tick (25fps) â€” utterance frame if queue has one, else idle frame. Background task `/attach`â†’`/stop`.
- **LMCache MP mode** (cross-replica KV-cache, env-togglable `LMCACHE_ENABLED`, task #47):
  - 4th ECS Service "lmcache-server" (EC2 c7g.2xlarge Spot ~$30/mo test, c7g.4xlarge on-demand ~$195/mo prod), CPU-only no GPU, `desired_count=1` (single, shared by all LLM replicas, NO scale).
  - `lmcache server --host 0.0.0.0 --port 5555 --l1-size-gb 8 --eviction-policy LRU --chunk-size 256` (port 5555 ZMQ + 8080 HTTP metrics).
  - LLM container: `PYTHONHASHSEED=0` MANDATORY (else hash key mismatch â†’ 100% miss), `LMCACHE_CONFIG_FILE=/app/lmcache_config.yaml`, vllm serve add `--kv-transfer-config '{"kv_connector":"LMCacheMPConnector","kv_role":"kv_both"}'` + keep `--enable-prefix-caching` (Layer 1 always on).
  - `lmcache_config.yaml`: chunk_size 256, local_cpu true, remote_url `lm://lmcache-server.internal:5555`, remote_serde cachegen.
  - â ï¸ c7g.large (4GB RAM) INSUFFICIENT for L1 8GB â†’ c7g.2xlarge (16GB) minimum. Compromise L1=8GB for MVP; bump if cache-hit ratio low.
  - `LMCACHE_ENABLED=true` (test scale) / `false` (post-test cost-save: lmcache-server desired_count=0 $0, LLM drops kv-transfer-config, keeps Layer 1 prefix-caching 0 cost).

### F. Deployment â€” ECS, 3 GPU/CPU instances + 4 support services (Seoul)
- **Orchestration**: AWS ECS (free control plane, GPU via EC2 launch type). No K8s, no Colab, no Docker Compose for runtime. Compose only for local dev.
- **3 GPU/CPU instances** (verified AWS Pricing API 2026-07-10, Seoul):

| # | Role | Instance | GPU | VRAM | On-demand/mo | Spot/mo |
|---|---|---|---|---|---:|---:|
| 1 | LLM + TTS (colocate, share 1 GPU) | `g6.xlarge` | L4 | 24GB | $722 | $206 |
| 2 | Avatar | `g4dn.xlarge` | T4 | 16GB | $472 | $179 |
| 3 | Backend API (CPU only) | Fargate Spot ARM64 (Graviton) | â€” | â€” | $36 | $11 |

Support services (no GPU, CPU-only):
| Service | MVP container | Prod managed | Spot/mo | On-Demand/mo | Env toggle |
|---|---|---|---:|---:|---|
| LiveKit Server (SFU) | Fargate Spot | Fargate | $22 | $72 | always on |
| PostgreSQL (DB runtime) | **RDS** db.t4g.medium + gp3 100GB | RDS | $86 | $86 | always on |
| Redis (ChatQueue, locks) | **ElastiCache** cache.t4g.small | ElastiCache | $12 | $12 | always on |
| **LMCache-server** (cross-replica KV) | **EC2 c7g.2xlarge Spot ASG** (stateful, not Fargate) | c7g.4xlarge on-demand | $95 | $238 | **`LMCACHE_ENABLED`** |
| ALB + LCU + S3 + CloudWatch + DataTransfer | â€” | â€” | $56 | $56 | always on |
| Cloudflare Free (DNS + edge WAF/DDoS) | â€” | â€” | $0 | $0 | always on |
| Docker Hub public images (code+deps) | â€” | â€” | $0 | $0 | always on |

> Network: **public-subnet only, 2 public AZs** (ALB/RDS AWS requirement; workload still single-AZ pin; no NAT, no private subnet). Security = SG lock + RDS `publicly_accessible=false` + no SSH. Edge: **Cloudflare Free** (DNS + rate-limit + DDoS) replaces Route53 + AWS WAF. Registry: **Docker Hub public** (weights on S3, not baked into image). See `aws-architecture.md`.

**Total prod (Seoul, Spot MVP, LMCACHE_ENABLED=true)**: **~$699/mo** (validated `aws-pricing-seoul.csv` 2026-07-11, PASS 45/45). PROD On-Demand Multi-AZ LMCache ON: **~$1916/mo**. Full breakdown in `aws-architecture.md` §8.

### F.1 ECS structure (4 Task / 4 Service)
| Layer | Count | Content |
|---|---:|---|
| Cluster | 1 | whole infra |
| Capacity Provider | 2 | (a) EC2 Spot ASG GPU pool (g6+g4dn) + LMCache c7g ASG; (b) Fargate Spot for Backend + LiveKit only |
| Task Definition | 4 | Backend / **LLM+TTS (2 containers, 1 Task, share GPU)** / Avatar / **LMCache-server (CPU only)** |
| Service | 4 | 1 per Task, desired_count=1 MVP (LMCache desired_count=0 when `LMCACHE_ENABLED=false`) |

### G. Region â€” Seoul (ap-northeast-2), MVP + prod
**Verified AWS Pricing API 2026-07-10**. I chose Seoul for you (your money = my money):
1. **Cheaper than Malaysia by $286/mo (23%)** â€” Malaysia lacks g4dn (count 0 verified) â†’ Avatar must use L4 ($740 vs $472).
2. **Quota already requested** â€” Seoul 2 cases opened (G/VT 8 vCPU case 178329128100829, P 96 vCPU case 178329128900161).
3. **Has both g6 + g4dn** â€” Malaysia only g6.
4. **Latency OK** â€” Seoul-VN ~60-80ms RTT, livestream has buffer.
5. Mumbai cheaper $67/mo but farther + no quota â†’ not worth migrating.

Quota cases (Seoul, submitted):
- Running On-Demand G and VT instances: 8 vCPUs (case 178329128100829).
- Running On-Demand P instances: 96 vCPUs (case 178329128900161).

### H. GPU sharing (LLM + TTS, 1 GPU, 2 containers, 1 Task)
- Only LLM container declares `resourceRequirements: {type: GPU, value: 1}`.
- TTS container uses `NVIDIA_VISIBLE_DEVICES` env â†’ same GPU UUID.
- `--gpu-memory-utilization`: **LLM ~0.6 / TTS ~0.25** (LLM KV-cache heavier), ~0.15 buffer. Verify via VieNeu footprint (backlog).

### I. Autoscaling â€” metrics NOT %VRAM
- Two tiers: Service Auto Scaling (Task count) + Capacity Provider Managed Scaling (EC2 count).
- Triggers: `vllm:num_requests_waiting` (>5-10 sustained 30-60s) + `vllm:gpu_cache_usage_perc` (sustained >70-80%, % of KV-cache pool filled, bounded 0-100%, NOT %VRAM allocation).
- **Mandatory**: `MaxCapacity` (Service) + `MaxSize` (ASG) + CloudWatch Billing Alarm (2nd layer).
- Scaling "LLM+TTS" Service doubles BOTH (same Task Definition).

### J. ARM (Graviton)
- Backend API: âœ… Graviton `c7g`/`c8g` Fargate Spot ARM64 (saves 20-40%, Python/FastAPI ARM64 easy).
- GPU workload: âŒ NOT ARM (G5g old Graviton2, PyTorch/CUDA ARM64 immature, diffusers risky).

### K. AMI
- GPU instances: AWS Deep Learning AMI (Ubuntu, NVIDIA driver + CUDA pre-installed, no extra cost).
- Backend (if EC2): Amazon Linux 2023 ARM64 (free, minimal).
- Avoid Windows/RHEL/SUSE (license fees).

### L. Databases â€” 2 separate, per-service (NO cross-team share)
| DB | Owner | Stores | Endpoints |
|---|---|---|---|
| DB business | BE team SE | users, shops, products, orders, payments, staff | `/user/*`, `/shop/*` (team SE codes) |
| DB runtime | You | sessions, session_products (snapshot frozen), viewer_msgs, director_decisions, llm_call_log, tts_call_log, audit | `/sessions/*`, `/admin/*` (you code) |

- Postgres 16 + pgvector (product embeddings) + pg_trgm (VN fuzzy) + LISTEN/NOTIFY (event fanout).
- Redis 7: ChatQueue (`XADD chat:{sid}`), session locks, rate-limit, LMCache backend (P4).
- **No DB sharing** (anti-pattern: coupling, security, runtime dependency, replay mismatch). Push-based `/attach` + snapshot frozen.

### M. Endpoints â€” unified `/api/v1/` root
REST = default (no `rest/` prefix). WS + media prefixed (different protocol, different infra route).

| Group | Endpoint | Protocol | Prod caller | Your test caller |
|---|---|---|---|---|
| Sessions | `POST /api/v1/sessions` | REST | BE team SE | FE test |
| | `POST /api/v1/sessions/{id}/attach` | REST | BE team SE | FE test |
| | `POST /api/v1/sessions/{id}/stop` | REST | BE team SE | FE test |
| | `POST /api/v1/sessions/{id}/say` | REST | BE team SE | FE test |
| | `POST /api/v1/sessions/{id}/plan/create` | REST | BE team SE | FE test |
| Avatars | `POST /api/v1/avatars` | REST | BE team SE | FE test |
| | `GET /api/v1/avatars` | REST | BE team SE | FE test |
| | `PUT/DELETE /api/v1/avatars/{id}` | REST | BE team SE | FE test |
| | `POST /api/v1/avatars/{id}/idle/regenerate` | REST | BE team SE | FE test |
| Realtime WS | `WS /api/v1/ws/platform/{sid}` | WebSocket | BE team SE (platform adapter) | FE test (mock chat) |
| | `WS /api/v1/ws/control/{sid}` | WebSocket | BE team SE (event consumer â†’ FE) | FE test (consumer) |
| Media | `POST /api/v1/media/livekit/room/{sid}` | REST | BE team SE gets token, FE joins LiveKit | FE test joins LiveKit |
| | `GET /api/v1/media/frame/{sid}.png` | HTTP | debug | debug |
| Engines | `POST /api/v1/engines/tts` | REST | BE team SE | FE test |
| | `POST /api/v1/engines/llm` | REST | BE team SE | FE test |
| | `GET /api/v1/engines` | REST | BE team SE | FE test |
| Health | `GET /api/v1/health/ready` | REST | BE team SE + monitor | FE test |
| | `GET /api/v1/health/live` | REST | monitor | â€” |
| Admin | `/api/v1/admin/*` | REST | You (ops) | You |

`/user/*`, `/shop/*` = BE team SE + DB business. You do NOT code these.
`/ws/control/{sid}` = event stream API â†’ BE team SE (NOT direct FE). BE forwards to FE as they choose.

## 3. Risks / assumptions

1. `cyankiwi/Qwen3.5-4B-AWQ-4bit` must load in vLLM base text-only (`--language-model-only`).
2. vLLM-Omni fork + VieNeu-TTS-v2 must build + run on L4 24GB (Seoul g6) â€” currently verified only Colab T4 cu13.
3. Pre-bake weights vs mount/cache runtime (image size vs startup). `VieNeu-TTS-v2` + `neuphonic/neucodec` pre-download to avoid 600s orchestrator timeout.
4. VRAM budget: LLM 0.6 / TTS 0.25 / buffer 0.15 â€” measure VieNeu footprint.
5. Benchmark EchoMimicV3-Flash on T4 â€” fps real-time or need g6/Tensor Parallelism (task #49).
6. Benchmark AWQ INT4 vs INT8-INT4 before locking prod (task #48).
7. transformers <5.9.0 or shim â€” neucodec 0.0.6 top-level import issues on transformers 5.x.
8. nvrtc available at runtime for codec JIT.
9. Test Backend Docker image ARM64 on Graviton `c7g` before committing ARM.
10. **Before paying customer**: GPU Spot â†’ On-Demand or mixed base/weight.

## 4. Implementation plan (post-confirmation)

1. HTTP/SSE client wiring (openai SDK + httpx) replacing in-process engines.
2. ECS Terraform: Cluster + 2 Capacity Provider (EC2 Spot ASG GPU + Fargate Spot) + 4 Task Definition + 4 Service.
3. Dockerfiles: LLM (vLLM base + AWQ), TTS (vLLM-Omni fork + `[vieneu]` extra), Avatar (FastAPI + LiveKit SDK + selected renderer after benchmark), Backend (ARM64 Graviton).
4. vLLM + vLLM-Omni launch configs (ports, `--gpu-memory-utilization` 0.6/0.25, prefix caching, dtype).
5. LiveKit server container + `livekit-rtc` SDK integration (CĂ¡ch B avatar-server publish video, API publish audio).
6. Idle loop pre-render + push into LiveKit VideoSource.
7. Avatar `/avatars` CRUD + idle loop generation (mock PIL â†’ AvatarForcing/EchoMimic/EchoAvatar Phase F).
8. Backend API: rename endpoints `/lite/*`â†’`/sessions/*`, `/ws/*`â†’`/api/v1/ws/*`, `/mock/*`â†’`/api/v1/media/*`, add `/avatars/*`, `/engines/llm`, `/admin/*`.
9. Runtime data layer: `core/store/postgres.py` (asyncpg + pgvector), `core/store/redis.py` (ChatQueue Stream, locks, LMCache backend), LISTEN/NOTIFY.
10. Service Auto Scaling: `num_requests_waiting` + `gpu_cache_usage_perc` triggers, MaxCapacity/MaxSize, Billing Alarm.
11. GPU sharing: `NVIDIA_VISIBLE_DEVICES` + 1 container declares GPU resource.
12. LMCache integration (task #47).
13. AWQ INT4 vs INT8-INT4 benchmark (task #48).
14. AvatarForcing + EchoMimicV3-Flash + EchoAvatar benchmark on T4/L4 (tasks #51, #52).
15. Test strategy: unit (offline pytest), smoke (per-service health), E2E (FE test â†’ full pipeline), CI matrix.

## 4b. Handoff docs (AFTER implementation complete)

### D1. API Reference + Integration Guide
`docs/api-reference.md` â€” every endpoint: method, path, protocol, payload, response, cĂ´ng dá»¥ng, curl/wsClient vĂ­ dá»¥, error codes. PhĂ¢n nhĂ³m Session/Realtime/Media/Engines/Health/Admin. RĂµ FE/BE/internal. Giao team SE tĂ­ch há»£p.

### D2. Architecture + Workflow + Database Runtime (gá»™p D9)
`docs/architecture-and-workflow.md`:
- 3 instances + 3 support services (ECS, GPU sharing, Seoul).
- Workflow: start â†’ attach â†’ platform ingress â†’ Director cluster/score â†’ LLM/TTS/Avatar â†’ LiveKit publish â†’ FE subscribe.
- Internal: APIâ†”HTTP/SSE LLM/TTS/Avatar, APIâ†”Postgres, APIâ†”Redis, LISTEN/NOTIFY.
- DB runtime schema: sessions, session_products (snapshot), viewer_msgs, director_decisions, llm_call_log, tts_call_log, audit. Query pattern.
- Mermaid sequence diagrams (5 flows, prod + test path).

### D3. Task Boundary + Questions for Team
`docs/task-boundary-and-questions.md`:
- Team SE tasks: DB business, `/user/*`, `/shop/*`, platform adapter, FE dashboard.
- Your tasks: `/sessions/*`, `/ws/*`, `/media/*`, `/engines/*`, `/admin/*`, DB runtime, LLM/TTS/Avatar serve, LiveKit.
- Endpoints you do NOT code (corrected): `/user/login`, `/shop/*`.
- 8 questions for team SE:
  1. BE team SE stores `api_session_id` how to link DB business â†” DB runtime?
  2. Platform adapter WS or webhook? Token auth mechanism?
  3. FE team SE LiveKit JS SDK version? Server-side publish or FE publish?
  4. Voice `ref_audio_url`: BE team SE host where? CDN? Presigned URL?
  5. Products snapshot: full JSON or only product_id + BE join later?
  6. Shop owner auth: team SE JWT forward to API or API issues own token?
  7. Multi-shop: 1 user many shops â†’ `/attach` needs `shop_id` field?
  8. Livestream end: BE team SE calls `/stop` or API auto-stop on idle N min?

### D4. Interactive Workflow Diagram (HTML)
`docs/workflow-diagram.html`:
- 1 standalone HTML, full workflow diagram (team SE â†’ API â†’ internal â†’ LiveKit â†’ FE).
- Inject D1 + D2 + D3: click node â†’ expand detail.
- mermaid.js or vis.js (CDN, no build step).

### D5. Runbook Ops
`docs/runbook-ops.md`:
- Deploy: ECS service update, GPU check, weight pre-download.
- Debug: per-service logs, GPU memory, common errors + fix (OOM, transformers shim, neucodec import).
- Restart 1 service without losing session.
- Rollback: image tag, DB migration revert.
- Health cheatsheet.

### D6. Protocol Schema (merge into D2 if short)
`docs/protocol-schema.md`:
- HTTP/SSE schema LLM/TTS/Avatar.
- LiveKit signaling, track types, codec.
- WS event envelope `/ws/control` + `/ws/platform` (versioned, msg_id, ack).
- Error code catalog.

### D7. Config & Env Reference
`docs/config-env-reference.md`:
- Every env var: LLM_*, TTS_*, AVATAR_*, LIVEKIT_*, POSTGRES_*, REDIS_*, RENDER_BACKEND, DIRECTOR_*, ENGINE_*.
- Default, type, effect, when to override. Per-env (dev/prod) recommendations.

### D8. Testing Strategy
`docs/testing-strategy.md`:
- Pyramid: unit (offline pytest), integration (ECS dev), smoke (per-service), E2E (FE test â†’ full pipeline).
- CI matrix: Python, GPU tiers, engine presets.
- Mock/stub for offline. Load test (PlatformSimulator stress).

### Docs grouping
- D2 + D9: merged (workflow + sequence).
- D6 + D2: merge if short.
- Total ~7-9 files.

## 5. Status

âœ… **CONFIRMED 2026-07-10**. AWS pricing/arch closed 2026-07-11. Next: execute `../plans/00-implement-aws-stack.md`, then app backlog in `../plans/01-app-feature-backlog.md`.
