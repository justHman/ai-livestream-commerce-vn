# Backend API Split Report (OpenSpec 1.20)

## Module list (file → routes)

| File | Routes |
|------|--------|
| `core/api/health.py` | `GET /health/live` (no deps, always 200), `GET /health/ready` (checks `app.state.container` → 503 fail loud when missing; never calls external services). Mounted directly on the app by both `core/server.py` and `backend.bootstrap.app_factory` — excluded from the versioned v1 contract. |
| `core/api/v1/router.py` | Shared wiring: `ControlHub`, `V1Deps`, `init_deps`/`deps`, rate-limit helpers, `build_run_plan`, `AvatarStore`, models. Routes: `GET /health`, `GET /health/live`, `GET /health/ready`, `POST /media/livekit/room/{session_id}`, `GET /mock/frame/{sid}.png`, `GET /mock/video/{sid}.mjpeg`, `GET /mock/status/{sid}`. |
| `core/api/v1/sessions.py` | `POST /lite/start`, `/lite/say`, `/lite/interrupt`, `/lite/stop`, `/lite/attach`, `PATCH /lite/config`, `/lite/ingest`, `/lite/chat`; aliases `POST /sessions`, `/sessions/{sid}/say|interrupt|stop|attach|ingest|chat`, `/sessions/{sid}/plan/create`; helper `_persist_viewer_msgs`. |
| `core/api/v1/avatars.py` | `POST/GET /avatars`, `GET/PUT/DELETE /avatars/{avatar_id}`, `POST /avatars/{avatar_id}/idle/regenerate`; class `AvatarStore`. |
| `core/api/v1/voices.py` | `GET /engines`, `POST /engines/llm`, `POST /engines/tts`, `POST /engines/tts/preset`, `POST /engines/tts/preview`. |
| `core/api/v1/websockets.py` | `WS /ws/control/{session_id}`, `WS /ws/platform/{session_id}`. |
| `core/api/v1/admin.py` | `POST /debug/start|stop`, `GET /debug/status/{sid}`, `/debug/mock_products`, `/debug/mock_viewer_msgs`, `/debug/clusters/{sid}`, `POST /admin/sandbox/verify`, `GET /admin/config`, `GET /admin/health`. |
| `core/api/v1/__init__.py` | Re-exports `router` + parity surface (`V1Deps`, `ControlHub`, `AvatarStore`, `init_deps`/`deps`, `lite_start/say/stop`, `build_run_plan`, all schema models, `SANDBOX_LAYER_TIMEOUT_SEC`, `mock_video_mjpeg`, `_persist_viewer_msgs`); imports the 5 route modules so they register on the shared router. |

## v1.py remaining content

`core/api/v1.py` was converted into the `core/api/v1/` package (no standalone file remains). `router.py` holds only the shared wiring + health/media/mock routes (~917 lines, down from 2049). `core.api.v1` import surface unchanged — tests and `core/server.py` / `backend.bootstrap.app_factory.py` still do `from core.api import v1` and `app.include_router(v1.router)`.

## Behaviors preserved

- All 42 unique paths + 3 WS routes (45 total) identical to pre-split.
- Auth deps (`viewer_auth`/`admin_auth`/`debug_enabled_dep`/`validate_ws_token`), rate limits, error envelopes byte-identical.
- `core/api/auth.py`, `core/api/limits.py` untouched.
- Sandbox timeout monkeypatch parity: `core.api.v1.SANDBOX_LAYER_TIMEOUT_SEC` read through `_sandbox_layer_timeout()` in `admin.py`.

## Test results

- `uv run pytest core/tests/test_app_factory.py -q` → **19 passed**
- `uv run pytest core/tests/ -q --ignore=core/tests/test_commerce_clustering.py` → **546 passed, 2 skipped**
- `uvx ruff check core/api/ core/server.py` → **All checks passed!** (core/tests has 26 pre-existing errors identical to baseline commit a2136bf — smoke tests + old unused imports, untouched by this change)

## Commits

- `b219353` — ruff format baseline (cherry-picked, pre-existing)
- `a2136bf` — checkpoint: v1.py → package with router.py + parity re-exports
- `08d4cad` — module split (sessions/avatars/voices/admin/websockets) + `core/api/health.py` + health router mounts in both app factories
