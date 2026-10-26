# Host identity and social replies

`SOCIAL_REPLY_ENABLED=0` and `SOCIAL_MODEL_ENABLED=0` by default. Roll out
Runtime with both off, then API, then Studio. Enable templates only for an
authorized canary; model choice is a separate opt-in. Turning replies off also
fences prepared speech and guarded TTS/audio publication.

The internal start accepts `host_name` only with execution identity and the
internal token. API snapshots each cast host's database name in the claimed
runtime session. Runtime pins the name into its execution envelope and hash.
An existing session without a name never invents one. Editing a name applies
to the next session. Product facts/approval scope are unchanged.

Recognized whole comments: greeting, thanks, host compliment, wellbeing, host
name and host identity. Mixed commerce keeps the commerce path; abuse still
passes the existing safety gate before routing. Other personal/off-topic
comments retain existing silence rules. Unknown variants are skipped rather
than sent unboundedly to a model.

Social comments never enter product-demand scoring. One group of up to 200
members coalesces for three seconds; TTL is 15 seconds. Playback is serialized,
never interrupts, and only fills idle gaps/boundaries between selling parts.
Commerce questions, opening, introduction and closing take precedence. Quota
is reserved before generation: at least 20 seconds between social replies,
at most three per rolling minute, and 60 seconds per viewer. Failed or
cancelled attempts consume their reservation, preventing a failure storm.
Dispatch reanchors the reservation and viewer limit to actual playback; delayed
preparation cannot shorten the 20-second interval. A per-speech permit checks
TTL until the first media window and keeps the kill switch active thereafter.
Commerce is reconsidered at the playback boundary and again before first media.

Templates rotate without LLM. Optional model work has a two-second deadline
and can only return an index into approved complete replies; invalid output
falls back to a rotating template. Name/identity replies are always fixed.

Live-follow uses `turn_kind=chitchat`, `response_kind=chitchat`, the existing
member manifest/hash, and final answered status only after completed playback.
Reasons: `social_cooldown`, `social_capacity`, `social_expired`,
`social_disabled`, `social_unrecognized`. Cancelled/failed speech stays
unanswered. No viewer text/names are added to observation events.

Offline verification: `test_social_reply.py`, approved-speech active tests,
safety-gate active tests and live-follow tests. Real audio/live behavior still
requires the owner's authorized canary; offline tests do not verify Facebook
delivery or avatar rendering.
