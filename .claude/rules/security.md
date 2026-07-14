---
paths:
  - "core/api/**"
  - "core/server.py"
  - "core/store.py"
  - "core/livekit_tokens.py"
---

# Security

- Validate all user input at the system boundary. Never trust request parameters (WS query, REST body, path params).
- Use parameterized queries (asyncpg `$1`). Never concatenate user input into SQL or shell commands.
- Two auth planes: VIEWER (`BACKEND_API_TOKEN` → `/sessions/*`, `/avatars/*`, `/media/*`, `/ws/*`) and ADMIN (`ADMIN_API_TOKEN` → `/engines/*`, `/admin/*`, `/debug/*`). `APP_ENV=dev` + empty token disables auth; `prod` + missing token = 401; valid viewer token on admin endpoint = 403.
- WS token validated via query parameter BEFORE `ws.accept()` — never after.
- Backend-only secrets: `LIVEAVATAR_API_KEY`, LiveKit API secret, DB password. Browser receives only `livekit_url` + `livekit_client_token`. Never send server secrets to the browser.
- Use constant-time comparison for tokens. Never log secrets, tokens, passwords, or PII.
- CORS whitelist via `CORS_ORIGINS` (not `*` in prod). Rate-limit auth endpoints.

# Security

- Validate all user input at the system boundary. Never trust request parameters.
- Use parameterized queries. Never concatenate user input into SQL or shell commands.
- Sanitize output to prevent XSS. Use framework-provided escaping.
- Authentication tokens must be short-lived. Store refresh tokens server-side only.
- Never log secrets, tokens, passwords, or PII.
- Use constant-time comparison for secrets and tokens.
- Set appropriate CORS, CSP, and security headers.
- Rate-limit authentication endpoints.
