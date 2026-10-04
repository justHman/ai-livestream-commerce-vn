# Runtime failure detection — P0-FB-020 R1

Default off: `RUNTIME_FAILURE_DETECTION_ENABLED=0`. With the switch off no
detector task, callbacks, counter response or capability is added. No readiness
gate is added, and no feature is advertised.

Enabling requires the approved-speech gate, coordinator and the already wired
durable 019 terminal producer. In a complete environment enable 019 storage and
producer, then 018 lease enforcement, then the API supervisor, as the 020 brief
requires. This document authorizes no deployment or provider operation.

`RUNTIME_UNHEALTHY_TICKS=5` and `RUNTIME_TERMINAL_ERROR_TICKS=20` count consecutive
tick exceptions. They are temporary implementation defaults requiring validation;
the latter must exceed the former. A successful tick resets the count and records
healthy recovery. Persistent exceptions fail with the classified runtime fault.
Expected content rejection, Hold, expiry and stale approved work keep their
existing safe refusal paths. Terminal preparation and exhausted speech-provider
errors invoke the same failure path; a successful existing safe fallback does not.

Only registered P0 execution identities are affected. Late provider errors with
an obsolete director revision are discarded. Failure blocks new speech, cancels
the approved epoch and invalidates queued work synchronously. Provider buffers
are interrupted independently of state/outbox I/O. The public execution becomes
FAILED only after its atomic fenced state save; a store outage retains pending
work and the independent API supervisor/018 lease remains the crash backstop.

Terminal usage facts are deferred in the same metadata save, preserving earlier
unresolved facts. The existing safety-stop pending/settling completion marker
retains cleanup and the durable terminal write across a crash. The 017 sender
retains and drains usage facts while PostgreSQL is unavailable. R1 retries use
bounded detached attempts and capped backoff; failures log a stable error class
only. `/health/ready` reports `runtime_failures` counters as information without
changing readiness. Inspect evidence/cleanup pending counts before operator
closure. Do not delete unresolved metadata or outbox evidence.

Validation here uses local doubles; LemonSlice, LiveKit and Facebook behavior
remain unverified until task 023 is explicitly approved and run.

Accepted fatal work participates in the locked rescue command and Stop decision before successful terminal persistence or metadata deletion. A failed promotion save keeps pending work and aborts Stop. Shutdown likewise attempts bounded promotion before its 019 terminal pass; an already durable terminal remains immutable.

