# Change B contract freeze — provider-neutral TTS runtime contract

Recorded 2026-08-12 by cluster-9b implementer (task 16.5). This is the
frozen provider-neutral contract Change B (`approved-script-authoring-pipeline`)
consumes. It supersedes any earlier assumption that Change B talks to
vLLM-Omni or a VieNeu-specific API: Change T owns the provider seam, and
Change B sees only this surface.

## Synthesis

`POST /v1/speech` is the canonical backend-facing path; `POST /v1/audio/speech`
is an alias to the same handler. There is no `/batch` endpoint — batching is
service-owned (the scheduler coalesces internally), so Change B must POST
per chunk.

Request body (all optional fields default safely; unknown fields rejected):

| Field | Type | Notes |
|---|---|---|
| `text` | string, 1..4000 | Chunk text (Change A `TextChunk.text`) |
| `voice_profile_id` | string | Opaque, tenant-scoped; `default` if omitted |
| `style` | string | `natural` (default), `news`, `storytelling` (provider-dependent) |
| `priority` | string | `normal` (default) / `high` |
| `session_id` | string | Bounded id (<=128) |
| `utterance_id` | string | Bounded id (<=128) |
| `chunk_seq` | int >= 0 | Monotonic per utterance |
| `response_format` | enum | `pcm` (default) / `wav` |
| `voice` / `language` / `speed` / `sample_rate` | legacy | Accepted for backward compat |

Response: audio bytes (WAV container for `wav`, raw int16 PCM for `pcm`) plus:

| Header | Meaning |
|---|---|
| `X-Request-Id` | Request correlation id |
| `X-Session-Id` | Echo of request `session_id` |
| `X-Utterance-Id` | Echo of request `utterance_id` |
| `X-Chunk-Seq` | Echo of request `chunk_seq` |
| `X-Audio-Sample-Rate` | e.g. 48000 |
| `X-Audio-Duration-Ms` | Encoded audio duration |

## Capabilities

`GET /v1/audio/capabilities` — provider-neutral facts:

```
provider_name, model_revision, sample_rate_hz (48000),
supports_native_batch, max_batch_size, supports_voice_cloning,
supports_mixed_voice_batch, supported_styles[], supported_expressive_cues[],
supported_response_formats[] (["pcm","wav"])
```

Provider-specific payloads (speaker embeddings, reference codes) never appear.

## Error semantics

Envelope: `{"error": {"code", "message"}}`.

| HTTP | Code prefix | Meaning |
|---|---|---|
| 400 | `provider_CapabilityError` / `validation_error` | Unsupported style/format/cue |
| 403 | `provider_ProfileUnauthorizedError` | Profile exists, tenant lacks access |
| 404 | `provider_ProfileNotFoundError` | Profile does not exist |
| 408 | `provider_DeadlineExceededError` | Request missed its deadline |
| 422 | `validation_error` / `invalid_reference_audio` | Malformed body / bad enrollment audio |
| 429 | `provider_OverloadError` | Admission bound exceeded (global 512, per-session 64) |
| 502 | `provider_ProviderInferenceError` / `engine_error` | Provider inference failed |
| 503 | `provider_ProviderUnavailableError` / `engine_unavailable` | Service/provider not ready |

## Voice profiles

`POST /v1/voices` (create), `GET /v1/voices` (list), `GET /v1/voices/{id}`,
`DELETE /v1/voices/{id}`. Profile ids are opaque and tenant-scoped; the
default profile is always available.

## Readiness

- `GET /health` — process liveness only (always 200).
- `GET /ready` — readiness: 200 when engine and scheduler runtime are ready,
  503 `engine_unavailable` otherwise.

Change B should gate synthesis on `/ready` before starting a live session.

## Notes

- Change A `TextChunk` fields map 1:1 onto request fields: `session_id`,
  `utterance_id`, `seq -> chunk_seq`, `text`, `is_final` (no wire field; the
  service keeps finality server-side). See `tests/integration/
  test_change_a_integration.py` (16.3).
- `X-Request-Id` is a fresh server-generated id per POST; never reuse a
  chunk's identity as an idempotency key.
