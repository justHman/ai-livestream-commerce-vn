# TTS service test inventory (OpenSpec 1.50/1.52)

Migrated from `core/tests/` — tests target the canonical `tts.*` package.
`test_audio_chunking.py`, `test_config.py`, `test_engine_selection.py` are
the pre-existing canonical unit tests (kept); the migrated engine tests were
named uniquely to avoid basename collisions across service suites.

## unit/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_audio_chunking.py | AudioChunk pcm16 conversion, TTSRequest defaults | new (pre-existing) | unit |
| test_config.py | EngineConfig/SecurityConfig validation, hosted-adapter rejection | new (pre-existing) | unit |
| test_engine_selection.py | Self-host ENGINES registry, tone fallback, unknown rejection | new (pre-existing) | unit |
| test_tts_streaming.py | ToneEngine stream_audio windows, TextChunk metadata, warmup, sample-rate preservation | core/tests/test_tts_streaming.py | unit |
| test_tts_presets.py (moved 1.50-fix1) | 6-preset registry, engine mappings, apply_tts_preset, TTSConfig preset-wins | core/tests/test_tts_presets.py | moved to backend_service (preset owner = backend engine_manager) |

## integration/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_health.py | /health/live + /health/ready truthfulness | new (pre-existing) | integration |
| test_speech.py | /v1/speech PCM/WAV, validation, voices, auth | new (pre-existing) | integration |

## contract/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_tts_v1.py | Committed OpenAPI matches built app, excludes health, engine 503 | new (pre-existing) | contract |

## Dropped deliberately

| Legacy source | Behavior | Why |
|---|---|---|
| core/tests/test_tts_remote_client.py (test_tts_remote_engine.py) | remote_http registration, synthesize PCM/WAV, HTTP error | the tts_service rejects hosted adapters (test_engine_selection.py asserts `remote_http` NOT in ENGINES); the canonical remote transport moved to the backend control plane — see backend_service/tests/unit/test_tts_self_hosted_client.py (backend.application.clients.tts.SelfHostedTTSClient) |

## Gap list

None — every legacy TTS test file is covered above.
