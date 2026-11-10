# BYOK avatar (Runtime)

A shop may bring its own avatar-provider account (LemonSlice, Simli, Beyond Presence, Tavus, Anam, Runway). The API (S1) seals
the shop's key for one Runtime start request; the Runtime opens it into memory, uses it for every
provider call of that session, and drops it when the session ends. Platform behaviour is unchanged
when the flags are off.

Contract: `023-prep/BYOK_AVATAR_CONTRACT_2026-11-01.md` (API addendum is authoritative for the wire).

## Env vars (S2)

| Var | Default | Meaning |
|---|---|---|
| `AVATAR_BYOK_ENABLED` | off | Accept tenant credentials. Builds the provider router around `RENDER_BACKEND`. |
| `AVATAR_CRED_TRANSIT_KEY` | none | base64 of 32 bytes, same value as S1. Required when the flag is on (the Runtime refuses to start otherwise). Never print it. |
| `AVATAR_CRED_TRANSIT_KEY_ID` | `t1` | Must equal the envelope `kid`. |
| `AVATAR_INTERNAL_TOKEN` | existing | Also guards `POST /internal/v1/avatar/credential-check` (header `X-Livento-Internal-Token`). |

Simli and LemonSlice tenant sessions use the existing `SIMLI_*` / `LEMONSLICE_*` settings
(LiveKit, timeouts) with the shop's key; the platform keys are not required for them.

The deploy needs `uv sync` once: `cryptography` is a new dependency (AES-256-GCM).

## How it behaves

- Session start (`POST /api/v1/sessions`): `avatar_credential_source=tenant` needs the envelope plus
  `avatar_credential_session_id` / `avatar_credential_tenant_id` (the AAD ids). Any envelope problem
  is HTTP 400 `AVATAR_ENVELOPE_INVALID`; the precise reason is only in the log as a word.
- Tenant sessions go to the backend of `avatar_provider`; platform sessions go to `RENDER_BACKEND`.
- Provider failures of a tenant session surface as `error.code`: `AVATAR_KEY_INVALID`,
  `AVATAR_OUT_OF_CREDIT`, `AVATAR_NOT_FOUND`, `AVATAR_PROVIDER_UNAVAILABLE`,
  `AVATAR_CONCURRENCY_LIMIT` (HTTP 502). A live that loses its key or credit ends through the
  existing `avatar_stop_reason` path with the same code.
- Platform guards (credit pause, daily avatar budget) ignore tenant sessions; tenant cost rows carry
  `credential_source: tenant` and the real provider label.
- Check endpoint: `POST /internal/v1/avatar/credential-check` returns `{code}` (`ok`, `invalid_key`,
  `not_found`, `unknown`, `provider_error`), never starts a billable session.
- Logs never carry the key or the envelope; the structured log filter also scrubs
  `api_key=...` / `avatar_credential_envelope=...` in free text.

## Wave 2 providers (Beyond Presence, Tavus, Anam, Runway)

Tenant-only backends in `clients/avatar/byok_providers.py`; a tenant session starts only with its
own key (a platform session is refused). Each is hidden until its flag is on. UNVERIFIED against
the real services: the module docstring lists the assumptions per provider (BEY*, TAV*, ANA*, RUN*, W*).
Do not enable a flag in production without a successful spike.

| Var | Default | Meaning |
|---|---|---|
| `BEYOND_PRESENCE_ENABLED` / `TAVUS_ENABLED` / `ANAM_ENABLED` / `RUNWAY_ENABLED` | off | Provider selectable for tenant sessions (needs `AVATAR_BYOK_ENABLED`). |
| `BEYOND_PRESENCE_API_BASE` | `https://api.bey.dev` | |
| `TAVUS_API_BASE` | `https://tavusapi.com` | |
| `TAVUS_DEFAULT_PAL_ID` | `pb87e71797da` | Stock echo PAL sent when `avatar_params.pal_id` is absent. |
| `ANAM_API_BASE` | `https://api.anam.ai` | |
| `RUNWAY_API_BASE` | `https://api.dev.runwayml.com` | |
| `<P>_AVATAR_IDENTITY` | `bey-` / `tavus-` / `anam-` / `runway-avatar-agent` | LiveKit identity we mint for the avatar (`<P>` = `BEYOND_PRESENCE`, `TAVUS`, `ANAM`, `RUNWAY`). |
| `<P>_AUDIO_SAMPLE_RATE` | 16000 bey/Runway, 24000 Tavus/Anam (`0` = default) | Rate of `lk.audio_stream`; our PCM is resampled to it. |
| `<P>_MAX_SESSION_S` | 3600 | Runtime near-cap guard; Tavus/Runway also send it (+60 s) as the provider cap. |
| `<P>_IDLE_BOUND_S` | 120 | End bound of a derived cost row. |
| `WAVE2_AVATAR_START_TIMEOUT_S` / `WAVE2_AVATAR_REQUEST_TIMEOUT_S` | 30 / 15 | |

`avatar_params` (optional object in the start body, max 4 keys, values `[A-Za-z0-9_.-]{1,64}`,
unknown keys rejected): Tavus `pal_id`, Anam `avatar_model`, Runway `kind` = `avatar` (default) or
`preset`. How a session ends: Tavus `POST /v2/conversations/{id}/end`, Anam
`POST /v1/sessions/{id}/stop`, Runway END_CALL data message + `DELETE /v1/realtime_sessions/{id}`,
Beyond Presence no end API (the room is left; the cost row is derived). The credential check uses
a read-only list call per provider (bey `GET /v1/avatars`, Tavus `GET /v2/conversations?limit=1`,
Anam `GET /v1/avatars`, Runway `GET /v1/organization`); a non-auth 4xx answers `unknown`.
## Turn it off

- Stop accepting tenant keys: unset `AVATAR_BYOK_ENABLED` (or set to `false`) and restart the Runtime.
  Platform sessions are unaffected; running tenant sessions end with the restart (stop_all).
- Hide a wave 2 provider: unset its `*_ENABLED` flag.
- Rotate the transit key: change `AVATAR_CRED_TRANSIT_KEY` and `_ID` on S1 and S2 together; envelopes
  live at most 150 s, so no session is affected.

## Unverified

Nothing here ran against a real provider. See the docstrings of `clients/avatar/simli.py` (S1-S10)
plus: the Simli `GET /faces` list shape used by the check endpoint.
