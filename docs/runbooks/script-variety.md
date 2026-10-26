# Complete-session script variety

The ordered batch audits the selected parts of every product, in owner order,
before emitting batch completion. Existing/skipped and human-edited content
participates in the audit but is never automatically rewritten. Only AI parts
produced by this batch are eligible, preserving their original write budgets.

Normalization keeps Vietnamese accents. Three-token part and sentence openers
have separate counters: three parts across the session, or two with the same
role, make a repetition group. Repeated sentence openings inside one part are
warnings. Exact approved fact/name/price spans are exempt, including legacy
plans without typed briefs. The original source text is never normalized in
storage. A narrow commitment-opener alias also detects known variants.

At most min(3, ceil(0.2 * part_count)) parts receive one additional model attempt.
The reservation shares the initial three-write budget per part and the original
batch deadline. A durable stable-part attempt marker precedes provider dispatch,
so recovery cannot repeat an ambiguous paid call or reset the global limit.

Repairs may only exchange short neutral sentence lead-ins. Sentence bodies,
numbers and price strings remain intact; existing role/fact/coverage/duration
and full-script guards run again. A candidate is accepted only if aggregate
excess decreases and no new repetition group appears. A failed/unsafe/expired
repair keeps the previous valid version; it never triggers a content fallback.

The original brief, selection and item revision are rechecked after provider
work, and artifact commits use the existing batch lease and item-revision
transaction fences. Cancelled work stops before further model dispatches.
The `BatchState.variety` receipt stores attempted stable keys, call count,
initial/remaining excess and completion. Remaining style warnings do not fail
otherwise valid factual content. No migration is needed (existing JSON state).

Explicit single-part regeneration receives opening context from all products
and cannot add new repetition groups or increase excess. It retains its own
existing three-write operation budget and keeps the selected version on failure.

Tests cover detectors, exact-span exemptions, Unicode accents, manual/skipped
content, accepted/rejected repairs, original budget sharing, recovery markers,
and the ordered-batch integration using the real content gate and fake LLM.
