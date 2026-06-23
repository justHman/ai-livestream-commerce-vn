# TASKS — VN Live-Commerce Host (implementations/)

## Done (this refactor)
- [x] `core/config.py` — AppConfig (env) + RENDER_BACKEND / SESSION_STORE selectors
- [x] `core/store.py` — SessionStore: InMemory (Colab) | Redis (AWS)
- [x] `core/render/base.py` — RenderBackend ABC (start/say/interrupt/stop), frontend-safe StartResult
- [x] `core/render/cloud.py` — CloudRenderBackend wrapping liveavatar_api (+ configure for LLM/TTS)
- [x] `core/render/self_host.py` — stub (NotImplemented), proves the seam
- [x] `core/api/v1.py` — /api/v1 routes + WS control hub
- [x] `core/server.py` — FastAPI app, env-wired, mounts /api/v1
- [x] `core/tests/v1_smoke_test.py` — sandbox v1 smoke
- [x] Migrate `liveavatar_api/frontend/lite.html` → /api/v1
- [x] `colab_deploy.py` → serve `core.server:app`, inject via core.render.cloud.configure
- [x] `notebooks/bootstrap_colab.ipynb` — clone→install→weights→move→env→smoke→run→ngrok
- [x] `datasets.yaml` + `notes/validation-report-2026-06-22.md` (16/16 VERIFIED)
- [x] `PRODUCTION.md`, `PLAN.md`, `TASKS.md`
- [x] Verify: core v1 + legacy smoke pass on sandbox; self_host NotImplemented confirmed

## Next phase
- [x] Director module (`core/director/`): bi-encoder clustering, phase scoring, 5-challenge FSM,
      StreamConfig (dashboard-admin writable), traffic-mode hook pool (gen-at-init + rotate)
- [x] Wire Director into the say-loop: `/api/v1/lite/attach` + `/lite/ingest` (comments -> Decision
      -> backend.say). Two-tier retrieval: TIER1 semantic product match + TIER2 O(1) structured-field
      lookup that GROUNDS an LLM prompt (fast + exact + natural host phrasing).
- [x] Model-agnostic TTS seam (`core/tts/`): TTSEngine ABC + registry + adapters
      (vieneu[default]/kokoro/cosyvoice/xtts) + offline tone engine. Swap model by config, not code.
      `colab_deploy.build_tts` now loads via core.tts (removed the `from vieneu import` anti-pattern).
- [ ] VERIFY on GPU: confirm VieNeu official runtime import/call (neuttsair vs vieneu) against the
      model card; confirm Kokoro/CosyVoice call surfaces. Adapters isolate this to one file each.
- [ ] Real LLM loader run on Colab GPU: llama.cpp GGUF (gemma-3-4b / Qwen3-4B Q4_K_M).
- [ ] Self-host diffusion RenderBackend — research done (see notes 2026-06-22):
        #1 Live Avatar (Quark-Vision/Live-Avatar, Apache-2.0) = open-source of the cloud we use;
           multi-ref + AR-infinite + anti-drift built-in, but needs >=48GB (FP8) / 2xA100 pooled;
           RTF on A100 UNVERIFIED -> benchmark before commit. Good for batch-streaming (offline RTF~1).
        fallback Ditto (Apache, ~0.2B, RTF<1 on 1 GPU) — single-ref, weaker anti-drift.
        API-only/closed (cannot self-host): OmniHuman-1, EMO/EMO2, Loopy.
        lip-sync is language-agnostic (audio-driven) -> no VN finetune needed.
- [ ] Optional: DBSCAN/HDBSCAN clustering option (current: greedy online cosine). Keep greedy for
      realtime; HDBSCAN only for POST-STREAM analysis (whole-session insight).
- [ ] AWS lift: Redis store, ALB sticky sessions, vLLM + prefix caching, KEDA/DCGM autoscale.
- [ ] PRODUCTION step: evaluate Go rewrite of the API/control plane (keep Python for finetune +
      model inference; Go talks to vLLM/TTS over HTTP). Not for Colab demo.

## Notes
- Keep `liveavatar_api/backend/*` untouched so its standalone smoke tests stay green.
- Secrets backend-only; browser gets only livekit_url + livekit_client_token.
- All dev on sandbox avatar (0 credits); free tier has 10 credits total.
