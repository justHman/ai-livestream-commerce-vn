# Comment guard

## Asymmetric costs: admission and speech are separate

Losing a real buyer question costs more than passing an uncertain comment to the
Runtime. After the existing safety/authentication/delivery checks, intake skips
only certain social shapes, with final reason `non_question`: complete laughter,
emoji/symbol/number-only text, acknowledgments such as `ok`, bare greetings,
complete generic compliments without an object, repeated characters and known
keyboard runs. Whole-comment matching preserves mixed greetings/product requests.
Unknown Unicode letters are not treated as emoji or meaningless text.

Intake never consults `question_kind` or personal/non-commerce rules. Every other
safe comment enters the normal counting, Studio observation, Coordinator, reducer
and Director path, with its original text. Personal and non-commerce comments are
accepted too. A later speech refusal does not undo intake acceptance.

## Speech and fallback authorization

`question_kind` belongs to the speech boundary. Its order is safety/social,
private-life veto, then positive commerce evidence. A narrow host-addressed shape
(pronoun + optional co + optional bao nhieu + spouse/partner/child/age/birth year/
hometown/preference/past intention/single status/location/occupation) vetoes
speech even when price, quantity or shipping words occur later. Existing direct
host questions about eating preferences, sleeping, drinking, dancing, health,
etc. remain bounded host-predicate shapes; there is no free scan for `an` through
food/product requests. A buyer's name or family recipient is not such a shape.
The veto precedes LLM and TTS work, including mixed clusters.

Original accented `c\u00f2n` is kept distinct from a child (`con`) before folding.
ASCII `con hang`, `con size`, `con mau` and inventory `con ban` favour commerce.
Thus `chi co con ban ao khong?` passes, as does its accented stock equivalent.
Food plus COD/payment also passes. Spouse + giao hang and bao nhieu + nguoi yeu
remain private-life questions and cannot authorize a fallback.

A fixed missing-information fallback needs explicit commerce evidence: price,
size/colour/material, shipping/stock, purchase/order/quantity/product code,
discount, payment/COD, returns/warranty, concrete goods or shop contact requests.
Bare `bao nhieu` without product/price evidence, a city, a question mark or an
incidental word in approved facts cannot authorize a fallback. A personal member
vetoes a whole cluster before this test. `unclassified` input still reaches the Director, but speech preparation refuses
it as `not_answerable_non_commerce` before LLM/TTS work. Positive commerce evidence
is required to authorize speech. An intact approved product phrase also counts
as positive evidence; incidental overlaps with facts never count.

The existing fixed commerce/current-product/other-product fallback texts never
echo viewer text or borrow facts from other goods. `is_deflection` recognizes all
fixed texts, including historical personal deflections for compatibility; new
personal deflections are never generated.

Speech refusals use final `live_follow.comment.skipped` with
`not_answerable_non_commerce` (Studio's existing skipped/Khong tra loi mapping).
They do not mark a response answered or update topic cooldown. The regression
uses actual reducer membership before preparation to verify this outcome; a
synthetic Decision without member IDs cannot represent a Studio comment.

## Safety matching and resources

Curated filenames remain stable. Toxicity publishes version **5**, harassment
version **3** and unsafe-content its existing version via provenance and
`resource_versions`. Toxicity v5 adds accented and ASCII `du ma may`. No external
dataset, runtime dependency, or resource is removed. Profanity lexicons for
*generated speech* are separate from viewer intake safety.

Matching preserves original viewer text and uses bounded tokens/phrases, NFC,
accent folding, basic leet, elongation reduction and bounded symbol/letter joins.
Strong curated multiword phrases are checked before ambiguous singleton
protection. `khon nan`, `do khon nan`, `do . khon . nan`, `khon kiep` and
`du ma may` therefore survive singleton protection. `du ma` alone remains
ambiguous. Explicit benign accents retain their meaning in phrase matching.

Phrases involving `cho` require the actual original `ch\u00f3` accent. ASCII
`do cho`, `cai do cho` and targeted ASCII equivalents never infer this accent.
The distinction is preserved through symbol joins too. Neither `cai do` nor a
hostile address can turn goods *for* a child into an insult. Accented abusive
phrases still match. ASCII `con khon` can mean a clever child; it uses the
protected path rather than strong-phrase precedence.

Original benign accents (ngu/large/enough/evening/flexible/wearing/machine/code,
etc.) remain protected. Ambiguous ASCII singleton homographs need additional
insult evidence. Strong profanity/abbreviations and explicit threats retain
token boundaries, preserving admin/account/acc and ordinary merchandise.
Never log rejected text: use existing fingerprints and reason codes.

## Repetition and routing

New purchase event IDs bypass topic cooldown; existing handled-ID suppression
remains. The Director checks per-session `deflection_interval_sec` at dispatch,
including prefetch (default 90 seconds; finite positive
`DIRECTOR_DEFLECTION_INTERVAL_SEC` override). Suppressed members receive final
`deflection_rate_limited`, remain skipped and add no topic cooldown. Actual
approved answers stay eligible during that interval. One spoken cluster receives
one shared response.

## Regression coverage and changed expectations

`tests/unit/test_comment_guard_round5.py` adds four authored groups: 120 benign
goods/recipient comments, 100 stock/food/payment commerce comments, 202 private-life
questions (including commerce decoys), and 145 strong-phrase abuse variants.
Every group has at least 80 new comments, with accented/ASCII and symbol cases.
All groups use authenticated HTTP through the real ASGI app and safety gate,
with real Coordinator/reducer calls observed. Privacy tests also run reducer
membership, speech preparation and final Studio-event assertions, and assert no
LLM/TTS or topic cooldown. Separate tests check missing-answer commerce fallback
preparation, ASCII cho symbol joins, and toxicity resource version 5 in evidence.
The reviewer supplied literal reproductions, not the full private corpus; these
results do not claim to rerun its undisclosed 768/160/244 inputs.

Policy-driven updates to older tests:

- `test_comment_guard_review.py`: personal HTTP assertions now require acceptance
  and routing, then final speech refusal with real reducer membership.
- `test_comment_guard_round2.py`: social comments still skip intake; personal
  comments must be admitted and refused only at speech preparation. Unclassified
  speech now also requires positive commerce evidence before any LLM/TTS work.
- `test_comment_guard_round3.py`: personal comments now pass intake. Ambiguous
  ASCII cho/con khon entries leave the abuse-only corpus; accented counterparts
  remain, and round five explicitly verifies ASCII cho admission.
- `test_comment_guard_round4.py`: personal comments now pass intake. Five former
  price/shipping-overrides-privacy examples move from SALES to PERSONAL. ASCII
  `co con ban` entries move to SALES because stock/child ambiguity favours buyers;
  accented child predicates stay personal.
- `test_live_answer_regressions.py`: a deictic-only request formerly authorized
  fallback via `cai nay`; it now stays unclassified and refuses fallback because
  it contains no hard commerce evidence. Intake remains open.

Verification commands run in `services/product/backend_service`, with
`UV_CACHE_DIR=D:/LiventoWorkspace/.uvcache`:

```
uv run --frozen --group dev python -m pytest tests/unit -q
uv run --frozen --group dev ruff check src tests
uv run --frozen --group dev ruff format --check src tests
```

Windows TEMP/TMP may point to a writable workspace directory; never use
`--basetemp`. Format only task files when needed, never the repository root.

## Known limits

Ambiguous ASCII intentionally favours buyers, even when it might be abuse:
`do cho`, `con cho`, `oc cho`, `con khon`, `do ngu`, machine/code/workwear
homophones, and tricks that erase the distinguishing abusive accent can pass.
The narrow private-life grammar is not a semantic classifier: unseen wording,
dialect, code switching and mixed clauses may miss the veto or suppress a real
commerce fallback. ASCII `con ban` can also refer to a child; it intentionally
favours stock/sales. These uncertainties never reject safe comments at intake.
Safe unclassified comments reach the Director but cannot emit speech without
positive commerce evidence. No corpus establishes universal coverage.
