# Avatar service test inventory (OpenSpec 1.50/1.52)

Migrated from `core/tests/` — tests target the canonical `avatar.*` package.
`test_config.py`, `test_engine_selection.py`, `test_session_state.py` are
the pre-existing canonical unit tests (kept); the migrated media tests were
named uniquely to avoid basename collisions across service suites.

## unit/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_config.py | EngineConfig/PublishingConfig/SecurityConfig validation | new (pre-existing) | unit |
| test_engine_selection.py | AvatarForcing from_config/lifecycle, unknown session | new (pre-existing) | unit |
| test_session_state.py | SessionManager browser-safe DTO, interrupt/stop/cleanup | new (pre-existing) | unit |
| test_avatar_windows.py | AudioWindow/VideoWindow/TextChunk dataclasses, split_waveform, merge_small_chunks, num_frames_for | core/tests/test_audio_windowing.py | unit |
| test_avatar_mock_lifecycle.py | MockRenderBackend start/stream/stop/status lifecycle | core/tests/test_mock_render_lifecycle.py | unit |
| test_avatar_mock_frames.py | JPEG frame generation, animation, get_last_frame_png | core/tests/test_mock_frame_generation.py | unit |
| test_avatar_idle_loop.py | Idle loop pre-render, wrap, iter_idle_frames, KeyError | core/tests/test_idle_loop.py | unit |
| test_avatar_livekit_publish_sdk.py | AudioTrackPublisher start/publish/stop via fake transport seam | core/tests/test_livekit_publish_sdk.py | unit |
| test_avatar_livekit_publish_stub.py | publish_enabled flag/creds, noop without env, loud SDK-required error | core/tests/test_livekit_publish_stub.py | unit |
| test_render_stop_all.py | MockRenderBackend.stop_all snapshot/delegate, stateless noop | core/tests/test_render_stop_all.py | unit |

## integration/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_health.py | /health/live + /health/ready truthfulness | new (pre-existing) | integration |
| test_avatars.py | /v1/avatars discovery, /v1/sessions lifecycle, no secret leak, 404, auth | new (pre-existing) | integration |
| test_livekit_publish.py | mint_room_token claims/errors, publisher client_token scoping | new (pre-existing) | integration |

## contract/

| Test file | Behaviors covered | Legacy source | Owner |
|---|---|---|---|
| test_avatar_v1.py | Committed OpenAPI matches built app, excludes health, engine 503 | new (pre-existing) | contract |

## Dropped deliberately

| Legacy source | Behavior | Why |
|---|---|---|
| core/tests/test_render_backend_enum.py (cloud/self-host selector types) | CloudRenderBackend/SelfHostRenderBackend isinstance checks | Those classes were removed in the split; the selector contract now lives in the backend (`backend/tests/unit/test_render_backend_enum.py`) |
| core/tests/test_remote_avatar.py (in-process RemoteAvatarBackend streaming) | remote stream_audio/interrupt | Replaced by the backend-owned SelfHostedAvatarClient HTTP transport (`backend/tests/integration/test_self_hosted_client.py`) |

## Gap list

None — every legacy avatar-owned test behavior is covered above (either in
this service or in the backend where the canonical code moved).
