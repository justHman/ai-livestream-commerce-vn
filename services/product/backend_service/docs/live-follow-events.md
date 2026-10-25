# Live-follow events (Runtime contract, schema_version 1)

Additive facts the Runtime emits so the API can show the shop owner (a) which part of
the approved script the host is on and (b) what happened to every comment. The Runtime
only observes: it does not change cluster selection, spoken content, safety or approval
gates, and adds no LLM call.

## Switches and operations

| Item | Value |
|---|---|
| `RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED` | `1/true/yes/on` enables emission, the membership observer and the snapshot. **Default off**: no event, no observer, snapshot returns `{"schema_version":1,"enabled":false}`. Read once when the coordinator is built (restart to change). |
| Transport | Existing `/ws/control/{session_id}` hub. The API bridge relays any type as `ai.runtime.<type>` (it has no allowlist; the gateway/Web whitelist for the new types is PR2/PR3 work). |
| Non-blocking | Events go through a per-session bounded queue (512) drained by a short task; errors are logged and swallowed. An overflow drops the oldest event and increments `dropped_events` in the snapshot, so the API must treat a `follow_sequence` gap or `dropped_events > 0` as "reconcile from the snapshot". |
| Privacy | IDs, codes and counts only. No comment text, viewer name, prompt, unit text (only its sha256) or upstream error text. |
| `DIRECTOR_MAX_QA_CLUSTERS_PER_WINDOW` | Separate knob (default 3, clamped 1..5). `qa_window_hard_timeout_sec=45` is still the ceiling of a Q&A window. |

## Envelope (every event)

```json
{
  "type": "live_follow.<name>",
  "schema_version": 1,
  "source_event_id": "<stream_epoch[:8]>:<fact>, unique per fact and epoch, e.g. 7324264f:turn:<turn_id>:completed:1",
  "tenant_id": "...", "business_session_id": "...", "runtime_session_id": "...",
  "generation": "execution_contract generation",
  "stream_epoch": "random per coordinator session start",
  "follow_sequence": 17,
  "source_occurred_at": "RFC3339 UTC", "emitted_at": "RFC3339 UTC"
}
```

* `follow_sequence` is strictly increasing per runtime session within one `stream_epoch`.
  A new `stream_epoch` (Runtime restart / re-attach) restarts the sequence: reconcile.
* `tenant_id/business_session_id/generation` come from the session `execution_contract`;
  they are `null` only if the session has none (non-P0), in which case scope by the bridge.
* Idempotency: upsert by `source_event_id`; per entity use the revision fields below.

## Events

### `live_follow.comment.cluster_assigned`
Current comment -> cluster mapping, batched (<= 50 changes/event).
```json
{"membership_revision": 1, "changes": [{
  "comment_id": "c1", "event_id": "evt-c1",
  "comment_key": "facebook/acc-1/live-1/msg-c1",
  "cluster_id": "cl-1", "previous_cluster_id": null,
  "change": "assigned", "cluster_revision": 1}]}
```
`change` is `assigned | reassigned | removed` (`cluster_id` is null for `removed`).
`comment_key` = `platform/connected_account_id/external_session_id/source_message_id`,
each URL-quoted. `event_id` is the platform event id the API already knows;
`comment_id` is the Runtime id used in every other fact. Reassignment only changes the
current mapping; it never rewrites a past turn's members.

### `live_follow.cluster.turn_selected`
Emitted when the Director queues a turn (Q&A or script unit); members are frozen here.
```json
{"turn_id": "turn-qa-1", "turn_kind": "qa", "action": "answer_cluster",
 "product_id": "serum", "unit_index": null,
 "cluster_id": "cl-1", "cluster_revision": 1, "membership_revision": 1,
 "topic_code": "shipping", "member_count": 2,
 "member_ids": ["c1", "c2"], "member_ids_truncated": false,
 "member_ids_sha256": "..."}
```
`turn_kind`: `qa | script | opening | closing`. Script turns carry only
`turn_id/turn_kind/action/product_id/unit_index`. `member_ids` holds at most 200 ids
(the cluster store bound); beyond that `member_ids_truncated` is true and the sha256
(sorted, length-prefixed ids) is the manifest hash. `topic_code` is from the allowlist
`price | shipping | stock | size_color | order | promotion | comparison | usage |
complaint | product_question` (the generic code for an unknown intent; there is no
`exchange` intent in the Runtime classifier). Comments assigned after this event are
not members of the turn and never inherit its outcome.

### `live_follow.turn.completed | turn.cancelled | turn.failed`
One terminal per `turn_id` (first terminal wins; a late failure never overwrites completed).
```json
{"turn_id": "turn-qa-1", "turn_kind": "qa", "attempt": 0, "outcome_revision": 1,
 "reason_code": "completed", "product_id": "serum",
 "cluster_id": "cl-1", "member_count": 2, "member_ids_sha256": "...",
 "topic_code": "shipping", "coverage_scope": "cluster",
 "completed_at": "RFC3339", "response_kind": "answer"}
```
* `completed` only after the speech pipeline finished and `mark_spoken` ran (selected /
  speak_started are never "answered"). Q&A: `response_kind` = `answer | deflection`
  (deflection = the validated speech text equals one of the fixed "chua du thong tin /
  ngoai le" deflection texts; the speech boundary has no structured kind yet, so a change
  of those texts must update `answer_context.is_deflection`); script turns:
  `script`. `coverage_scope` is always `cluster`: never per-member coverage.
* `cancelled` reason_code: `interrupt | hard_expired | superseded | content_rejected |
  session_stopped | cancelled`. `failed`: `failed | playback_timeout | preparation_failed`.
* `attempt` is the speech attempt that ended the turn (internal transient retry).
* Join `member_ids` of the matching `turn_selected` (or the snapshot turn). If a
  terminal arrives before its `turn_selected`, wait/reconcile; compare
  `member_ids_sha256`.

### `live_follow.comment.deferred` / `live_follow.comment.skipped`
```json
{"comment_ids": ["c2"], "event_ids": [], "reason_code": "topic_recently_answered",
 "final": false, "related_turn_id": null}
```
`comment_ids` (Runtime ids, <= 200 per event) or `event_ids` (comments refused at intake,
which never got a Runtime id). `reason_code` allowlist: `topic_recently_answered,
unclassified, expired, non_actionable, duplicate_content, safety_filtered,
session_closing, session_closed, capacity_exceeded, delivery_failed, turn_cancelled,
turn_failed`; anything else is sent as `unknown` with `final=false` (never upstream text).

Emitted today (others in the allowlist are reserved for the API/future work):

| Fact | Event | reason_code | final |
|---|---|---|---|
| Director cooldown wait (`topic_cooldown`) | deferred | `topic_recently_answered` | false (reported once per comment; size_gate is not reported: the comment just waits) |
| Turn cancelled/failed, member still in store | deferred | `turn_cancelled` / `turn_failed` | false (Director may select it again -> new turn) |
| Turn cancelled/failed, member already left the store | skipped | `turn_cancelled` / `turn_failed` | true |
| `hard_expired` cancel | skipped | `expired` | true |
| Left the store, older than the 75 s horizon, unanswered | skipped | `expired` | true |
| Left the store earlier (cluster/member bound), unanswered | skipped | `capacity_exceeded` | true |
| Left the store after a cooldown deferral | skipped | `topic_recently_answered` | true |
| Cluster intent spam/off_topic/social (never selectable) | skipped | `non_actionable` | true |
| Runtime SafetyGate refusal at intake | skipped (`event_ids`) | `safety_filtered` | true |
| Stale / replay-flood refusal at intake | skipped (`event_ids`) | `expired` / `duplicate_content` | true |
| Execution fence `closing` / `ending` | skipped | `session_closing` / `session_closed` | true |
| Coordinator stop | skipped | `session_closed` | true |

Not emitted by the Runtime (API-side or out of scope here): moderation-service
rejections, API-side delivery failures (`delivery_failed`), routed/not_ready/non_deliverable
outcomes (the API already has them), reducer pending-queue overflow (500), and
`unclassified`. Comments consumed by a torn-down session after `stop()` cannot be
announced (the hub is gone): the API's non_deliverable ledger covers them.

### `live_follow.position.updated`
Emitted only when the position changes.
```json
{"position": {
  "phase": "answering_comment",
  "turn_id": "turn-qa-1", "turn_kind": "qa", "action": "answer_cluster",
  "product_id": "serum", "unit_index": null, "unit_count": 10,
  "unit_text_sha256": null, "part_id": null,
  "segment": {"script_set_id": "ss-1", "envelope_hash": "...", "approved_version_id": "ver-9"},
  "script_cursor": {"product_id": "serum", "next_unit_index": 3, "unit_count": 10, "completed_units": 3},
  "topic_code": "shipping", "cluster_id": "cl-1", "member_count": 2,
  "media_started": false, "hold_reason": null}}
```
* `phase`: `preparing` (turn selected / next unit pending), `speaking` (speak_started
  of a script turn), `answering_comment` (speak_started of a Q&A turn), `holding`
  (execution Hold), `idle` (nothing left to speak). `speaking` means the Runtime started
  the pipeline, not that viewers hear it: `media_started=true` is set when the first audio
  window passed the boundary (still not Facebook playback).
* `script_cursor` is the Director truth: `next_unit_index` advances only when a script
  unit completes (`mark_spoken`), never for Q&A, retry, cancel or hold. While answering,
  show the script position from `script_cursor` / the interrupted unit and the topic from
  `topic_code`.
* The current part is identified by `segment` (version) + `unit_index` of the bound
  approved script. `unit_text_sha256` lets the API verify the text it shows; the Runtime
  does not send the text. `part_id` is always `null` for now: units are blank-line
  paragraphs of the approved spoken text and are not proven to equal an editor part, so
  the API/Web must label by unit.

## Recovery snapshot

`GET /api/v1/sessions/{runtime_session_id}/live-follow` (viewer auth; 404
`unknown_session` if not attached). Bounded: 200 most recent comments, 10 most recent
turns (<= 200 member ids each).
```json
{"schema_version": 1, "enabled": true, "tenant_id": "...", "business_session_id": "...",
 "runtime_session_id": "...", "generation": "...", "stream_epoch": "...",
 "watermark": 17, "dropped_events": 0, "membership_revision": 1, "observed_at": "RFC3339",
 "position": { "...same object as position.updated..." },
 "comments": [{"comment_id": "c1", "event_id": "evt-c1", "comment_key": "...",
   "state": "answered", "reason_code": null, "final": true,
   "turn_id": "turn-qa-1", "cluster_id": "cl-1", "updated_sequence": 10}],
 "turns": [{"turn_id": "turn-qa-1", "turn_kind": "qa", "status": "completed",
   "attempt": 0, "outcome_revision": 1, "cluster_id": "cl-1", "topic_code": "shipping",
   "member_count": 2, "member_ids": ["c1", "c2"], "member_ids_truncated": false,
   "member_ids_sha256": "...", "completed_at": "RFC3339", "response_kind": "answer"}],
 "limits": {"comments": 200, "turns": 10, "member_ids_per_turn": 200}}
```
`state`: `pending` (waiting) | `deferred` (waiting, `reason_code` set, not final) |
`selected` (frozen into a turn that has not completed) | `answered` (completed turn,
`turn_id`; read `response_kind` from the turn) | `skipped` (`reason_code`, `final`).
Apply events with `follow_sequence > watermark` after loading a snapshot; on a
`stream_epoch` change or sequence gap, reload. The snapshot lives in Runtime memory: a
Runtime restart loses it (the API's own persisted projection is the durable record).

## Behaviour notes

* Retire (stop / re-attach) drops queued facts, including unsent closing outcomes,
  and cancels the drain and its in-flight hub send. A replacement shares the session's
  send lock and waits for that send to unwind before emitting its own facts. This
  prevents an old application send from completing after a new-generation send.
  Bytes already handed to the transport/kernel cannot be recalled: consumers must
  scope by the authoritative `stream_epoch`, reject retired epochs and apply increasing
  `follow_sequence` within that epoch (reconcile on epoch changes; do not switch back
  to an epoch already retired).
* Live comment metadata (platform timestamp, key and state) is pinned to reducer
  membership, bounded by its configured `max_active_clusters * max_members_per_cluster`
  (defaults: 40 * 200). Separate history keeps at most 2000 departed/other comments;
  turns are bounded at 40, and cluster revisions only cover live clusters.
* Closing/ending is re-applied every tick and on every membership sync, so a comment that
  arrives under the fence is also finalized.
* Expiry vs capacity uses the platform timestamp the recorder keeps from the first time it
  sees a comment. A comment that is already older than the 75 s horizon when the reducer
  batch runs is expired before the recorder ever sees it and gets no event.
* `DIRECTOR_MAX_QA_CLUSTERS_PER_WINDOW` default 3 changes Director behaviour even with the
  live-follow flag off (product-owner request): up to 3 Q&A clusters per window, not 2.

## UI mapping hints (for API/Web)

pending -> "Dang cho AI"; selected -> "AI dang chuan bi tra loi chung"; answered +
`answer` -> "AI da tra loi chung (cung N binh luan ve <topic_code>)" (N = turn
`member_count`; N=1 -> "AI da phan hoi"); answered + `deflection` -> "AI da phan hoi,
chua du thong tin"; skipped final -> "Khong tra loi" + short reason (`safety_filtered`
-> "Da loc"); deferred -> "Dang cho"; anything unknown or missing -> "Chua xac nhan".
