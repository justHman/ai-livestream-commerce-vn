# LemonSlice cost guards — 2026-10-12

PR #99 enabled idle healing from the keepalive loop without distinguishing a prepared
Runtime from a broadcast. A ready execution could replace its provider session every
`LEMONSLICE_ROLLOVER_AFTER_S` forever; HTTP 402 followed the normal 40-second retry path.
The incident reported for stream `70fabaf4…` / Runtime `dd46eb0c…` is consistent with
those paths. The supplied counts are incident context, not independently measured billing.

## Runtime behavior

- Keepalive/heal runs only in `live`, `warming`, `selling`, `closing`, or within 120 seconds
  of an utterance. A session waiting in preparing/ready sends no idle resets and no background
  replacements. Its configured LemonSlice idle timeout applies. A new utterance recreates an
  absent avatar even when timed rollover is disabled. Active PCM/playback is never rolled over.
- `RUNTIME_READY_TTL_S=1800` expires a Runtime that has never spoken or entered an on-air
  phase. Zero disables this TTL. Provider rollover never resets the Runtime creation time.
  A lifespan-owned watcher checks every five seconds, independently of the optional failure
  monitor. It fences speech, persists the existing failed execution shape, then uses the full
  stop path (comment reconciliation, avatar termination, publisher, director, terminal/usage
  persistence, metadata). Cleanup failures retain retry work with a 60-second pause.
- `LEMONSLICE_DAILY_AVATAR_MINUTES=0` disables the additional limit by default. A positive
  value uses `REDIS_URL` as a shared ledger, partitioned by a SHA256 fingerprint of the
  LemonSlice API key. UTC calendar days; simultaneous avatars sum their elapsed wall minutes.
  Admission is atomic across replicas and refuses new POSTs once usage reaches the limit.
  Existing sessions are safety-stopped by the watcher at the limit. Redis failure refuses
  spending; it never switches to a local counter. Redis persistence/no-eviction is required.
  Admission and cleanup are not an exact provider invoice: the counter conservatively starts
  before the POST, charges unknown outcomes through the possible creation + idle expiry,
  and extends that lease before idle resets/audio (including queued playback). Positive
  provider idle timeout is required. A crash cannot erase earlier usage or accrue forever.
  There can be overshoot during the five-second poll and bounded cleanup, especially with
  concurrent avatars. This guard controls Runtime-owned avatars using the same ledger;
  other LemonSlice customers/processes are outside its accounting.
- HTTP 402 from creation or idle control trips `LEMONSLICE_CREDIT_EXHAUSTED` before any
  best-effort ledger cleanup. Logs explicitly say **LemonSlice hết credit**; response text
  begins with that fixed marker and contains no upstream body, key or URL. Existing executions
  are stopped; creation is refused for `LEMONSLICE_CREDIT_BACKOFF_S=3600` (minimum 300).
  New attempts after that cooldown can probe recovery. The cooldown is process-local;
  a restart may make one new probe, while daily accounting remains shared/durable.

## Existing API transport: no API changes

Audited API `origin/main` `c38775cd51c848742b807db2d283ebe20ef2d8a3`:

1. Runtime session creation returns HTTP 502 with `detail` as a short string.
2. ai-connector `newAIRuntimeHTTPError` preserves that string with a 256-rune bound;
   `RuntimeLifecycleConsumer.reportFailed` forwards the wrapped message (1000-byte bound).
3. stream `FailFromRuntime` stores the reason as `failure_reason` (Postgres TEXT).
4. Studio stream transport preserves the raw field. The companion Web PR displays a specific
   message only when the complete fixed marker is present; older error text keeps its fallback.

**Accepted limitation:** ongoing execution failures are classified as `media_failed` and API
callbacks/terminal convergence replace the detail with `execution_failed` or
`execution_failed:media_failed`. Credit-specific text therefore reaches Studio for preparation
and direct upstream errors, but cannot be promised for background rollover exhaustion. The
user explicitly chose to keep API unchanged after this lossy path was reported.

## GoLive/readiness and deferred creation proposal

Initial avatar creation stays eager in this patch. Allowing a ready avatar to idle out has an
existing-flow consequence: Facebook `PrepareGoLive` checks actual avatar tracks before the
opening utterance is permitted. A formerly ready session whose avatar expired can fail
GoLive until preparation is restarted. Generic readiness polling/token minting must not be
given billable side effects, because that would recreate the original unattended-spend loop.

Proposed follow-up (design only): explicit owner-triggered **GoLive preflight** with an
idempotency key and execution generation. Create one avatar immediately before launch;
wait for audio/video tracks; renew the current readiness source with the new track SIDs;
start/rebind egress and verify ingest; only then publish Facebook Live and activate opening.
Cancel, TTL and failure must terminate the reserved avatar and egress. Split content readiness
from short-lived media readiness, with a bounded preflight deadline and one concurrent attempt.

Risks: changed track SIDs invalidate source proofs; egress may retain old tracks; publishing
before ingest is confirmed produces a blank live; an ambiguous POST may leave a paid avatar;
double clicks/late callbacks can create two providers; mobile/network timeouts may strand launch.
This needs coordinated Runtime/API/media-join/Web contracts and provider/egress evidence.
It is deliberately not implemented as part of these two independent PRs.

## Verification

New tests cover ready idle gating/first utterance recreation, on-air silence/recent speech,
partial-speech interruption, TTL disable/live exclusion, P0 terminal cleanup retry, daily
UTC overlap/shared admission/restart/idle lease, invalid env values, HTTP marker preservation,
creation/rollover/control 402, and 402 during Redis cleanup failure. Existing live keepalive
tests now explicitly mark their test sessions on air. All provider tests use protocol doubles.
No live provider request, merge or deployment is needed for these checks.
