---
paths:
  - "services/product/backend_service/src/backend/api/**"
  - "services/product/backend_service/src/backend/application/render/**"
  - "services/product/backend_service/src/backend/application/director/**"
---

# Error Handling

- Raise typed/custom errors with codes, not generic `Exception("something went wrong")`. FastAPI: use `HTTPException` with consistent `{error: {code, message}}` shape.
- Never swallow errors silently. Log or rethrow with added context (which session, which engine, which stage of the pipeline).
- Handle every rejected asyncio task. No floating `asyncio.create_task` without exception handling — the streaming pipeline (LLM→chunker→TTS→video queue) runs in `asyncio.to_thread`; cancel via `threading.Event` and propagate cancel cleanly.
- HTTP error responses: consistent shape, correct status codes (400 validation, 401 auth, 403 forbidden, 404 not found, 409 wrong-instance, 410 session-lost, 503 engine/LiveKit unavailable).
- Self-host render backends (`self_host_avatarforcing_half`, `self_host_echoavatar_full`) must **fail loud** (`NotImplementedError` / explicit error) until the adapter exists — never silently degrade to mock.
- Retry transient remote errors (LLM/TTS/avatar HTTP/SSE timeouts) with bounded backoff. Fail fast on validation and auth errors — don't retry them.
- Include `session_id` / request context in logs when available.

# Error Handling

- Use typed or custom error classes with error codes, not generic `Error("something went wrong")`.
- Never swallow errors silently. Log or rethrow with added context about what operation failed.
- Handle every rejected promise. No floating (unhandled) async calls.
- HTTP error responses: consistent shape (`{ error: { code, message } }`), correct status codes (400 validation, 401 auth, 404 not found, 500 unexpected).
- Never expose stack traces, internal paths, or raw database errors in production responses.
- Retry transient errors (network timeouts, rate limits) with exponential backoff. Fail fast on validation and auth errors. Don't retry them.
- Include correlation or request IDs in error logs when available.
