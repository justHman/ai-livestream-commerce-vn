# LLM service test inventory (OpenSpec 1.50/1.52)

Migrated from `core/tests/` — tests target the canonical `llm.*` package.
`test_streaming.py`, `test_config.py`, `test_engine_selection.py` are the
pre-existing canonical unit tests (kept); the migrated engine tests were
named uniquely to avoid basename collisions across service suites.

## unit/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_config.py | EngineConfig/SecurityConfig validation, hosted-adapter rejection | new (pre-existing) | unit |
| test_engine_selection.py | Self-host ENGINES registry, noop load, unknown rejection | new (pre-existing) | unit |
| test_streaming.py | stream_chunks deltas/final markers, noop single chunk | new (pre-existing) | unit |
| test_llm_streaming.py | stream_chunks default/incremental, llamacpp errors | core/tests/test_llm_streaming.py | unit |

## integration/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_health.py | /health/live + /health/ready truthfulness | new (pre-existing) | integration |
| test_chat_completions.py | chat completions text/stream, models, 422, auth, 413 | new (pre-existing) | integration |

## contract/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_openai_compatible.py | Committed OpenAPI matches built app, excludes health, engine 503 | new (pre-existing) | contract |

## Dropped deliberately

| Legacy source | Behavior | Why |
|---|---|---|
| core/tests/test_llm_remote_client.py (test_llm_remote_engine.py) | openai_compat/remote registration, generate/stream, guided_json | the llm_service rejects hosted adapters (test_engine_selection.py asserts `openai_compat` NOT in ENGINES); the canonical remote transport moved to the backend control plane — see backend_service/tests/unit/test_llm_openai_client.py (backend.application.clients.llm.OpenAICompatibleClient) |
| test_llm_streaming.py LLMConfig.stream env + Qwen3.5 preset sections | LLMConfig.from_env/to_engine_cfg stream flag; AVAILABLE_LLM_PRESETS Qwen3.5 entry | those symbols live in the backend control plane (backend.config.LLMConfig, backend.engine_manager.AVAILABLE_LLM_PRESETS) — the llm_service has no LLMConfig and cannot import backend (dependency is backend→llm); moved to backend_service/tests/unit/test_llm_config_and_presets.py |

## Gap list

None — every legacy LLM test file is covered above.
