# Backend service test inventory (OpenSpec 1.50/1.52)

Migrated from `core/tests/` — every legacy core test file is accounted for
below (moved, consolidated, or explicitly dropped). Tests target the
canonical `backend.*` / `llm.*` / `tts.*` / `avatar.*` packages; no `core`
imports remain. The only guarded `core.*` fallbacks left in service src are
the pre-existing COPY-DON'T-IMPORT try/except seams at
llm/engines/base.py:34 and tts/engines/base.py:20 (service-local fallbacks).

## unit/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_comment_buffer.py | ChatQueue put/drain/stats/clear, max-size eviction, unique ids | core/tests/test_chat_queue.py | unit |
| test_comment_clustering.py | VN commerce routing, clustering partitions, hash baseline, semantic threshold | core/tests/test_commerce_clustering.py | unit |
| test_coordinator_multisession.py | Per-decision orchestrator factory, concurrent sessions, cancel isolation, decision score field | core/tests/test_coordinator_multisession.py | unit |
| test_director_coordinator.py | Coordinator start/tick/stop lifecycle, ingest, idempotent start, WS event secrecy | core/tests/test_director_coordinator.py | unit |
| test_director_timers.py | Timer bookkeeping, product-time budget, engagement decay, clock handling | core/tests/test_director_timers.py | unit |
| test_decision_preparation.py | Prepared-turn pipeline, retries, interrupt invalidation, Q&A variants, closing commit, cluster snapshot | core/tests/test_stage2_diagnostics.py | unit |
| test_director_decisions.py | Director FSM: opening/selling/closing, pivot hysteresis, excursion, answer-variant cache, traffic updates | core/tests/test_stage2_auto_demo_sequence.py | unit |
| test_pipecat_config.py | PIPECAT_ENABLED config gate (bridge stub behavior dropped — pipecat is config-only until wired) | core/tests/test_pipecat_bridge.py | unit |
| test_tts_presets.py | 6-preset registry, engine mappings, apply_tts_preset, TTSConfig preset-wins | core/tests/test_tts_presets.py | unit |
| test_llm_openai_client.py | canonical OpenAICompatibleClient chat/stream/retry via MockTransport | core/tests/test_llm_remote_client.py | unit |
| test_llm_config_and_presets.py | LLMConfig.stream env parsing + Qwen3.5 preset fields | core/tests/test_llm_streaming.py (moved from llm_service; LLMConfig/presets owned by backend) | unit |
| test_tts_self_hosted_client.py | canonical SelfHostedTTSClient synthesize PCM/WAV, HTTP error | core/tests/test_tts_remote_client.py | unit |
| test_playback_queue.py | BoundedVideoQueue drop-oldest, CoordinatorMetrics, StreamOrchestrator end-to-end, cancel, callback | core/tests/test_queue_coordinator.py | unit |
| test_queue_metrics.py | get_or_idle idle/underflow/emergency fallback, last_frame_age | core/tests/test_queue_metrics.py | unit |
| test_render_backend_enum.py | AppConfig.build_render_backend selector contract (mock local; cloud/self-host remote placeholder) | core/tests/test_render_backend_enum.py | unit |
| test_run_plan.py | RunPlan schema, build_run_plan determinism, coverage cursor, Utterance schema | core/tests/test_run_plan.py | unit |
| test_text_chunker.py | TextChunker punctuation/max/timeout/finalize flushes with fake clock | core/tests/test_text_chunker.py | unit |
| fixtures.py | Shared MOCK_PRODUCTS fixture (replaces core/debug/mock_data.py for service tests) | new | unit |

## integration/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_api_limits.py | SlidingWindowLimiter, MaxBodySizeMiddleware, REST/WS 429/413, CORS on 413, boundary models | core/tests/test_api_limits.py | integration |
| test_api_persist.py | pg persistence at start/ingest/chat, failure log hygiene, no-pg no-op | core/tests/test_api_persist.py | integration |
| test_api_security.py | Viewer/admin auth 401/403, debug gate 404, CORS '*' rejection, health public | core/tests/test_api_auth.py | integration |
| test_api_security_helpers.py | auth tokens_match/parse_bearer, WS token, rate limits, container isolation | core/tests/test_backend_api_security.py | integration |
| test_app_factory.py | create_app env-driven + injected deps, health live/ready, engine load errors | core/tests/test_app_factory.py | integration |
| test_app_factory_container.py | BootstrapContainer references, fresh container per app, missing container safe, no global deps | core/tests/test_bootstrap_container.py | integration |
| test_avatar_routes.py | /avatars CRUD + idle regenerate | core/tests/test_avatars_api.py | integration |
| test_control_websocket.py | /ws/control token auth, control.connected, ping/pong | core/tests/test_ws_auth.py | integration |
| test_embedder_readiness.py | hash/semantic embedder modes, health/ready embedder status | core/tests/test_embedder_readiness.py | integration |
| test_lite_chat_integration.py | /chat coordinator path, cluster snapshot, 404/413, stop drops session | core/tests/test_lite_chat_integration.py | integration |
| test_livekit_publishing.py | LiveKitPublisherRegistry lifecycle, stop ordering, shutdown, per-session publisher | core/tests/test_livekit_publish_registry.py | integration |
| test_livekit_token.py | mint_room_token claims, media/livekit/room endpoint, 503 on missing secret | core/tests/test_livekit_token.py | integration |
| test_mock_media_absent.py | /mock/* MJPEG routes absent from production app | core/tests/test_mjpeg_continuous.py | integration |
| test_platform_websocket.py | /ws/platform store/accept/reject, burst 1008, reconnect budget | core/tests/test_platform_ws.py | integration |
| test_postgres_runtime_store.py | runtime_schema.sql tables/indexes, store enabled/disabled | core/tests/test_postgres_schema.py | integration |
| test_postgres_store_lifecycle.py | PostgresRuntimeStore lifecycle with fake pool, persistence SQL | core/tests/test_postgres_store_lifecycle.py | integration |
| test_sandbox_route_absent.py | /admin/sandbox/verify absent from production app | core/tests/test_sandbox_verification.py | integration |
| test_self_hosted_client.py | SelfHostedAvatarClient start/stop transport, error mapping | core/tests/test_remote_avatar.py | integration |
| test_server_pg_lifecycle.py | lifespan pg connect/retry/close, shutdown ordering, readiness | core/tests/test_server_pg_lifecycle.py | integration |
| test_session_concurrency.py | concurrent say 200/409, lock release, interrupt mid-say, cloud say path | core/tests/test_session_concurrency.py | integration |
| test_session_config.py | /attach shop profile/order/revisions/runtime config validation | core/tests/test_editable_session_config.py | integration |
| test_session_routes.py | /sessions lifecycle, plan/create, admin config no-secrets, mock 404 | core/tests/test_sessions_api.py | integration |
| test_voice_routes.py | /engines status + TTS preset apply/404 | core/tests/test_engines_endpoint.py | integration |
| test_voice_routes_discovery.py | engines/avatars discovery, tts/preview WAV, failed swap preserves engine | core/tests/test_runtime_discovery_preview.py | integration |

## Moved out of backend ownership

| Legacy source | Destination | Reason |
|---|---|---|
| core/tests/test_benchmark_stage2.py | tests/e2e/test_benchmark_contracts.py | benchmark harness is a root tool (1.58 moves it to benchmarks/) |
| core/tests/test_liveavatar_playback_timeout.py | tests/sandbox/test_liveavatar_playback.py | provider-layer seam, sandbox-only |
| stage2 LiteConversation test (in test_stage2_auto_demo_sequence.py) | tests/sandbox/test_liveavatar.py | provider-layer seam, sandbox-only |
| core/tests/test_infra_database_url_secret.py | infra/tests/test_database_url_secret.py | Terraform check |
| core/tests/test_platform_service_roots.py | infra/tests/test_platform_roots.py | Terraform/platform check |
| terraform module-source test (in test_canonical_path_references.py) | infra/tests/test_module_sources_resolve.py | Terraform check |
| core/tests/test_llm_* / test_tts_* | llm_service/tests/ / tts_service/tests/ (except test_tts_presets.py — preset registry owner is the backend engine_manager; now backend_service/tests/unit/) | engine owner |
| core/tests/test_audio_windowing.py + mock render tests | avatar_service/tests/ | media-plane owner |
| core/tests/test_gitleaks_allowlist.py, test_workbench_token_gate.py, test_stage2_console_static.py, test_product_service_roots.py, test_service_ownership_map.py, test_canonical_path_references.py (retained remainder) | retained in core/tests/ | repo-structural checks; removed with core/ in cleanup 1.79 |

## Dropped deliberately

| Legacy source | Behavior | Why |
|---|---|---|
| core/tests/test_pipecat_bridge.py (is_enabled/build_pipeline/run_turn) | pipecat bridge stub | pipecat is a config-only toggle in the canonical backend (no bridge module exists until the feature is wired); the config gate test survives in test_pipecat_config.py |
| core/tests/test_llm_remote_client.py / core/tests/test_tts_remote_client.py | openai_compat/remote_http engine registration + MockTransport behavior | hosted adapters are rejected by llm_service/tts_service (their test_engine_selection.py asserts absence); the canonical remote transports live in the backend control plane — covered by test_llm_openai_client.py + test_tts_self_hosted_client.py |

## Gap list

No behaviors from `core/tests/` are left uncovered: the 476 service-migrated
tests are all present in the tables above; the 63 structural tests remain in
`core/tests/` until the 1.79 cleanup removes core itself.
