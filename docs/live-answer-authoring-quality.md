# Live answer and authoring quality

2026-10-14 customer evidence: shipping questions mentioning ordering matched
purchase intent; numbers in unrelated paragraphs caused an approved delivery
claim to disappear; prefetched prose stayed ahead of new questions; nested repair
layers increased model calls; ordered plans ignored requested duration.

Shipping intent now precedes generic purchase intent, and social greetings use
word boundaries. Claim coverage is checked per approved paragraph. Unambiguous
price/delivery questions can use intact approved answers with polite context from
actually completed speech and upcoming approved units, without a model call.
Mixed offers/fees/personal delivery guarantees still use the guarded existing path.
The original approved-speech safety fence remains active.

At a completed speech boundary the coordinator re-evaluates new demand before
playing prefetched proactive prose. A selected reply cancels only unplayed prepared
turns; active speech finishes and the real script cursor is preserved. This removes
an extra queued paragraph's wait, rather than promising an instantaneous interruption.

Ordered authoring uses the requested target, calibrated to Studio's existing
characters-per-second estimate, with spaced factual recaps when enough approved
facts exist. It never invents claims to fill the target. Up to three product workflows
can write concurrently; state aggregation/persistence stays on the owner thread.
Guard retry and gate repair share at most two semantic dispatches per part. Ordered
authoring disables nested transport retries; a durable reservation occurs before
provider dispatch and survives recovery. Ambiguous failures do not cause a third call.

Whole regeneration creates new immutable plans/versions in the current set and
restores the previous selected version on failure/cancellation. Single-part repair
pins the current source version and segment IDs, preserves siblings, and performs
both local and full-script gates. Recovery retains exact source/index in the durable
job and a separate per-operation model budget. Workflow reads verify set/product
ownership; raw provider exception messages are not exposed.

The API owns session action quotas and operator settings. Runtime schema additions
are additive: job input/error metadata and `script_model_write_budget`. Old source
without AI segment provenance cannot be safely repaired as a single segment.

Local tests use provider doubles. PostgreSQL and browser checks are isolated.
Actual generation latency, six-minute speech duration and live reaction must still
be measured with the configured provider after rollout; no paid acceptance run
has been made by the agent.
