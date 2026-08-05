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
| test_tts_remote_engine.py | remote_http registration, synthesize PCM/WAV via MockTransport, HTTP error | core/tests/test_tts_remote_client.py | unit |
| test_tts_presets.py | 6-preset registry, engine mappings, apply_tts_preset, TTSConfig preset-wins | core/tests/test_tts_presets.py | unit |

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

None. All `core/tests/test_tts_*` behaviors migrated.

## Gap list

None — every legacy TTS test file is covered above.
