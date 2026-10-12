# LemonSlice cost guards — 2026-10-12

PR #99 enabled idle healing from the keepalive loop without distinguishing a prepared
Runtime from a broadcast. A ready execution could replace its provider session every
`LEMONSLICE_ROLLOVER_AFTER_S` forever; HTTP 402 followed the normal 40-second retry path.
The incident reported for stream `70fabaf4…` / Runtime `dd46eb0c…` is consistent with
those paths. The supplied counts are incident context, not independently measured billing.

## Runtime behavior

- Preparing/ready sessions preserve their existing avatar with idle resets until
  `RUNTIME_READY_TTL_S=1200` (20 minutes). They never heal/recreate or roll over in the
  background, even when rollover age is exceeded. On-air (`live`, `warming`, `selling`,
  `closing`, or recent speech) retains heal/rollover behavior. First utterance can recreate
  a missing avatar. Active PCM/playback is never replaced.
- TTL counts from initial creation, never renewed by ready transitions/provider rollover.
  Twenty minutes accommodates observed 110-second preparation and operator preview while
  bounding unattended cost. Zero disables TTL explicitly. At deadline both keepalive loop
  and dispatch guard stop resets independently of the five-second watcher. The watcher fences
  speech, persists failed execution, then owns full stop: reconciliation, avatar/publisher/
  director, terminal/usage and metadata. Cleanup retry pauses 60 seconds.
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

### TTL notice without API changes

After full cleanup, Runtime retains at most128 TTL diagnostic tombstones for one hour.
GET execution for a deleted session returns ordinary HTTP404 with a short string detail
beginning `RUNTIME_READY_TTL_EXPIRED` and an instruction to prepare again. Existing ai-connector
GetExecution/upstream-error handler and Web transport preserve that detail. Web scopes notices
to tenant/stream/runtime and rechecks during preparation and60s after terminal status.
No enum/schema or API repo changes. Terminal convergence still loses custom details from
stream.failure_reason; the notice uses the existing execution-error route. After Runtime
restart, history expiry/eviction or long cleanup outage, generic fallback remains. Logs retain
an understandable TTL reason. Valid failed execution stays readable until cleanup completes.

## GoLive/readiness and deferred creation proposal

Initial creation stays eager. The prior candidate's no-reset rule was a readiness regression:
S2 idle timeout 30s ended the avatar before observed 110s preparation/media join. This revision
preserves that source while pre-live TTL is valid. Provider/network/GPU failures can still end
it: pre-live missing avatars do not heal automatically; failed readiness remains recoverable
through operator preparation, rather than uncontrolled paid resurrection.

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

A fake-clock journey runs the actual keepalive task against a double enforcing idle30s:
silent preparing140s, ready preview240s, warming/GoLive120s, first utterance at t=500.
Assertions: one creation, same participant/video/audio, 25 resets at20s intervals. Counterfactual
no-reset loses the avatar at31s. API EvaluateAvatarSource is checked independently against
journey snapshots without API commits. These doubles do not prove live-network availability.

Tests retain actual preparing/ready phases for legacy reset/teardown/error cases, and cover
missing-avatar/no-rollover, exact TTL reset cutoff, TTL disable/on-air exclusion, cleanup retry,
bounded TTL history, shared UTC/restart budget, 402 and ledger failures. No provider spend.
