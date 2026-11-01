# Simli backend (`RENDER_BACKEND=cloud_simli`)

Second avatar provider next to LemonSlice. Code: `services/product/backend_service/src/backend/application/clients/avatar/simli.py`
(subclass of the LemonSlice backend; the module docstring lists the UNVERIFIED assumptions S1..S10).
Default is unchanged: `RENDER_BACKEND=cloud_lemonslice` keeps working exactly as before.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `RENDER_BACKEND` | - | `cloud_simli` enables it (fails at startup without the key) |
| `SIMLI_API_KEY` | required | sent only as `x-simli-api-key` to `compose/token` |
| `SIMLI_API_BASE` | `https://api.simli.ai` | |
| `SIMLI_FACE_ID` | required unless a face is passed per session | Trinity-2 face id |
| `SIMLI_AVATAR_IDENTITY` | `simli-avatar-agent` | LiveKit identity of the avatar |
| `SIMLI_MAX_SESSION_S` | `3600` | Runtime near-cap guard; provider `maxSessionLength` = this + 60 s |
| `SIMLI_MAX_IDLE_S` | `120` | `maxIdleTime`; keep above the longest pause between utterances |
| `SIMLI_START_TIMEOUT_S` / `SIMLI_REQUEST_TIMEOUT_S` | `30` / `15` | ready wait / one POST |
| `SIMLI_STREAMING_WIDTH`, `SIMLI_STREAMING_HEIGHT` | unset | both, >= 512, ratio 1:1, 16:9 or 9:16 |
| `SIMLI_AVATAR_POSITION_X`, `SIMLI_AVATAR_POSITION_Y` | unset | 0..1 |
| `SIMLI_BACKGROUND_ID`, `SIMLI_FOREGROUND_ID`, `SIMLI_CAMERA_JSON` | unset | sent only when set |

Shared with LemonSlice: `LIVEKIT_*`, `LEMONSLICE_SPEAK_READY_*`, `LEMONSLICE_CREDIT_BACKOFF_S`,
`RUNTIME_READY_TTL_S`, `AVATAR_AUDIO_FALLBACK_PUBLISH`, `AVATAR_RENDER_OFFSET_MS`.
Cost evidence rows carry `provider="simli"` (seconds only; prices live in the API, BR-PRICING).

## Switch back

Set `RENDER_BACKEND=cloud_lemonslice` and restart the Runtime. No data migration.

## Limits to know

No terminate endpoint: stop = the Runtime participant leaves the room. No avatar replacement
(rollover) and no keep-alive: a vanished avatar or `near_cap` ends the live through the existing guards.

## Real-API spike (manual, spends free minutes)

```
uv run --project services/product/backend_service python scripts/simli_spike.py \
  --confirm-real-api --env-file <env-file> --audio <vi.wav> --report out/simli-report.json \
  --save-frames 6 --frames-dir out/frames
```
Add `--idle-seconds 60` (with default `--max-idle-s 20`) for the idle tests, `--soak-seconds N --max-total-s N+60`
for a long session. Output: one PASS/FAIL/INCONCLUSIVE line per scenario plus the JSON report.
