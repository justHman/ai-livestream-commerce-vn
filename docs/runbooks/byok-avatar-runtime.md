# BYOK avatar (Runtime)

A shop may bring its own avatar-provider account (LemonSlice, Simli, bitHuman). The API (S1) seals
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
| `BITHUMAN_ENABLED` | off | bitHuman selectable for tenant sessions. Unverified against the real service. |
| `BITHUMAN_API_BASE` | `https://api.bithuman.ai` | |
| `BITHUMAN_MODEL` | `essence-2` | |
| `BITHUMAN_API_KEY` / `BITHUMAN_AGENT_ID` | empty | Platform-account fallback only; BYOK sessions bring their own. |
| `BITHUMAN_MAX_SESSION_S` | 3600 | Runtime near-cap guard (provider cap unknown). |
| `BITHUMAN_IDLE_BOUND_S` | 120 | End bound of a derived cost row (provider idle unknown). |
| `BITHUMAN_START_TIMEOUT_S` / `BITHUMAN_REQUEST_TIMEOUT_S` | 30 / 15 | |
| `BITHUMAN_AVATAR_IDENTITY` | `bithuman-avatar-agent` | LiveKit identity of the avatar agent. |

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

## Turn it off

- Stop accepting tenant keys: unset `AVATAR_BYOK_ENABLED` (or set to `false`) and restart the Runtime.
  Platform sessions are unaffected; running tenant sessions end with the restart (stop_all).
- Hide only bitHuman: unset `BITHUMAN_ENABLED`.
- Rotate the transit key: change `AVATAR_CRED_TRANSIT_KEY` and `_ID` on S1 and S2 together; envelopes
  live at most 150 s, so no session is affected.

## Unverified

Nothing here ran against a real provider. See the docstrings of `clients/avatar/simli.py` (S1-S10)
and `clients/avatar/bithuman.py` (B1-B6), plus: the Simli `GET /faces` list shape and the bitHuman
`GET /v1/me` response used by the check endpoint.
