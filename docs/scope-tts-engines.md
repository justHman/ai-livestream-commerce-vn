# TTS Engines — Confirmed Scope

> Status: **CONFIRMED** (aligned 2026-07-11). Canonical engine table lives in `scope-engine-and-models.md` §2. This file is the short TTS-only companion for ops/UI.

## Production runtime (AWS)

| Priority | Preset id | Model | Serve | Notes |
|---|---|---|---|---|
| 1 (default) | `vieneu-v2-omni` | `pnnbao-ump/VieNeu-TTS-v2` | vLLM-Omni on g6 (shared w/ LLM) | Streaming crossfade; TTFB ~0.5s; fork `justHman/vllm-omni@feat/vieneu-tts-v0.22` |
| 2 | `gwen-tts-omni` | `g-group-ai-lab/gwen-tts-0.6B` | same Omni server swap | VN finetune; dual-track streaming |
| 3 | `voxcpm2-omni` | `openbmb/VoxCPM2` | same Omni server swap | Multi-lang + strong clone |

- **Not default for AWS:** in-process Colab adapters (tone / transformers-mms-vi / local VieNeu package). Those remain for offline mock / Colab only.
- **Hot-swap:** `POST /api/v1/engines/tts` restarts Omni task with new `--model` (true hot-swap not supported). UI reads `GET /api/v1/engines`.
- **Voice clone fields:** `ref_audio_url`, `ref_text`, `language`, `sample_rate` per avatar.

## Colab / offline presets (non-prod)

Registered for demo/dev only via `EngineManager` presets (see `architecture.md` §7):

`vieneu-v3-turbo`, `vieneu-v2`, `cosyvoice2`, `kokoro`, `xtts-v2`, `transformers-mms-vi`, plus tone stub.

## Adapter backlog (app code, not AWS infra)

Tracked in `plans/01-app-feature-backlog.md`:

- Keep Omni path primary for ECS.
- Optional in-process adapters for Colab remain secondary.
- Do not reintroduce head-only avatar TTS coupling; avatar is half/full-body video only.

## Forbidden / closed

- MMS-TTS as production default.
- Baking TTS weights into Docker image (weights on S3, entrypoint sync).
- Treating CosyVoice/XTTS as AWS default without Omni integration proof.
