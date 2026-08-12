# TTS Engines — Confirmed Scope

> Status: **CONFIRMED** (aligned 2026-07-11; runtime updated by Change T 2026-08-12).
> Canonical engine table lives in `scope-engine-and-models.md` §2. This file is the short TTS-only companion for ops/UI.

## Production runtime (AWS)

| Priority | Preset id | Model | Serve | Notes |
|---|---|---|---|---|
| 1 (default) | `vieneu-v3-turbo` | `pnnbao-ump/VieNeu-TTS-v3-Turbo` | Provider runtime on g6 (GPU share) | Scheduler-driven batching; `TTS_PROVIDER=vieneu_v3` |

**Historical (pre-Change T)**: the vLLM-Omni fork serving VieNeu-TTS-v2
(`justHman/vllm-omni@feat/vieneu-tts-v0.22`) is superseded by the
provider-neutral FastAPI service; see `multi-session-batched-tts-runtime`.

- **Not default for AWS:** in-process Colab adapters (tone / transformers-mms-vi / local VieNeu package). Those remain for offline mock / Colab only.
- **Voice clone fields:** `ref_audio_url`, `ref_text`, `language`, `sample_rate` per avatar.

## Colab / offline presets (non-prod)

Registered for demo/dev only via `EngineManager` presets (see `architecture.md` §7):

`vieneu-v3-turbo`, `vieneu-v2`, `cosyvoice2`, `kokoro`, `xtts-v2`, `transformers-mms-vi`, plus tone stub.

## Adapter backlog (app code, not AWS infra)

Tracked in `plans/01-app-feature-backlog.md`:

- Provider runtime path primary for ECS (provider seam; scheduler).
- Optional in-process adapters for Colab remain secondary.
- Do not reintroduce head-only avatar TTS coupling; avatar is half/full-body video only.

## Forbidden / closed

- MMS-TTS as production default.
- Baking TTS weights into Docker image (weights on S3, entrypoint sync).
- Treating CosyVoice/XTTS as AWS default without provider-runtime integration proof.
