# Avatar readiness and delivery recovery

LemonSlice speech readiness is enabled by default. Set
`LEMONSLICE_SPEAK_READY_GATE_ENABLED=false` to restore the previous delivery
behavior. `LEMONSLICE_SPEAK_READY_WAIT_S` defaults to 45 seconds and bounds one
readiness attempt, including provider rollover. These are Runtime settings;
this change does not alter deployed environment values.

Before each new utterance, including the opening and speech after recovery,
the Runtime requires the configured avatar participant and an unmuted video
publication in its LiveKit room. This proves publication, not decoded video
frames or provider health. PCM is withheld while waiting. Rollover still ends
the old provider and waits for its avatar to leave before creating another.
A creation which outlives the readiness deadline remains owned and serialized;
another retry cannot start a concurrent provider.

An unconfirmed playback or readiness timeout does not complete a script unit.
The coordinator retains the same decision, approved text and cursor during up
to two retries (three total attempts). An unconfirmed playback forces provider
replacement before replay; a confirmed playback is never replayed. A fresh
avatar flushes the channel ledger before reuse. After exhaustion, the position
is explicitly retired so the live can continue; it is recorded as skipped,
not as successfully delivered. QA answers are skipped immediately when already unready, otherwise get one attempt.
Retries revalidate approved speech and cancellation before sending media.

The bound applies to readiness, not TTS or playback: each playback retains its
audio duration plus configured playback margin. A script unit can therefore
wait up to three readiness budgets, plus TTS and playback time. Provider cleanup
may continue in the backend after a readiness timeout, with PCM withheld until
safe recovery. Delivery acknowledgement is not an exactly-once audience
receipt: some audio may have played before an acknowledgement was lost, so a
retry may repeat content, but replacement prevents simultaneous old/new speech.

## Live-follow contract (additive, schema version 1)

With `RUNTIME_LIVE_FOLLOW_EVENTS_ENABLED=true`, the existing
`live_follow.position.updated` event and snapshot `position.phase` may contain
`avatar_reconnecting`. Preserve `turn_id`, `unit_index` and `script_cursor`
while displaying **Người dẫn AI đang kết nối lại…**. Resume the usual speaking
phase when readiness returns. On exhaustion, a failed turn outcome clears the
reconnecting phase. Web rendering is a separate change.

`coordinator.retry_scheduled` uses `error=playback_unconfirmed|avatar_not_ready`.
`coordinator.avatar_delivery_skipped` carries `turn_id`, `unit_index`, `attempts`
and `reason` with the same reason codes. No successful `speak_finished` is
emitted for that skip. QA skip reasons are also normalized in live-follow.

INFO logs record provider start, room join/leave, start-to-video latency,
rollover reason (`age`, `absent`, `playback_unconfirmed`) and end, readiness
wait duration/result, and unconfirmed playback with provider age and time since
join. Times labelled `at` are Unix seconds; durations are monotonic seconds.
Room departure reason is `unknown` when the SDK supplies no cause. Logs contain
room/utterance IDs, never tokens, keys or image URLs. Provider start is observed
when creation returns; provider-internal timing and root cause remain unknown.
