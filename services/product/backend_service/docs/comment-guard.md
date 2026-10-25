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

`question_kind` authorizes missing-information fallback only. It must never
cancel preparation, skip a facts lookup, or force an other-product question
straight to fallback. Every admitted answer turn first tries the existing
answer-from-facts / natural-answer and approved-answer selection paths. Natural
answers still pass the existing grounding checks and LLM judge; selected answers
must equal a complete approved fact. A valid answer may be spoken regardless of
classification. The original viewer question is supplied to both model paths.

Only after those paths return empty/invalid (or approved facts/model are
unavailable) does `deflection_for` classify the question. Clear commerce permits
the existing fixed fallback; personal, social, safety-filtered or unclassified
questions remain silent, without TTS. This final refusal does not undo intake or
an already attempted model call. A personal member vetoes fallback for a mixed
cluster, while a valid approved-fact answer remains eligible.

The narrow private-life fallback veto recognizes host addresses including
`chi chu`, `chu shop` and `admin`, followed by bounded private predicates.
Accented wife and product-cover words are distinguished before folding. Bare
ASCII `vo` is a spouse only in `vo chong` or a terminal `co vo khong` shape;
`vo goi` does not veto fallback. Child questions such as terminal `co con khong`
and `co may con` differ from `co con dao` and `co con bup be`. Known child-action
shapes and existing direct host-private questions remain bounded rules.
Accented inventory and protective meanings remain distinct. Ambiguity favours
commerce fallback rather than a privacy veto.

Existing hard commerce evidence remains unchanged. Soft evidence adds selling
common goods, requests to show/view goods, and availability questions with common
product nouns (skirts, kitchen knives, pillow covers, sleepwear, food and toys).
Bare quantities, city names, punctuation or incidental words in facts do not
become commerce evidence. An intact approved product phrase still counts.

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

`tests/unit/test_comment_guard_round6.py` adds three deterministic corpora, each
with at least 100 new unique cases plus the reviewer's exact reproductions:
selling/viewing goods, child/cover product homographs, and host-private questions
with sales/shipping decoys. Accented and ASCII variants are explicit. Ambiguous
ASCII spouse-plus-sales clauses are deliberately in the commerce group.

Every corpus entry is tested through authenticated ASGI ingestion, actual reducer
membership and answer preparation, with both valid and empty controlled-model
outputs. Valid answers and commerce fallback are dispatched through real guarded
TTS. Silence asserts no TTS, no topic cooldown and a final Studio skipped event
with `not_answerable_non_commerce`. Extra cases verify invalid generated answers,
terminal child/spouse grammar and product-boundary counterexamples. The prior
abuse corpora and intake safety implementation are unchanged.

Policy-driven updates to older tests in this round:

- `test_comment_guard_review.py`, `test_comment_guard_round2.py`, `test_comment_guard_round3.py`,
  `test_comment_guard_round4.py`, `test_comment_guard_round5.py`: silence tests now
  supply empty model output and require the approved-fact path to have run. They
  no longer require classification to cancel before LLM work. Intake and abuse
  assertions remain intact.
- Rounds three/four/five move ambiguous ASCII `vo` plus sales clauses from PERSONAL to
  SALES; explicit accented wife questions remain personal.
- `test_shop_question_homographs.py`: a valid checked answer must survive even
  non-commerce classification; the test now verifies preparation and actual TTS.
- `test_shop_fallback.py`: other-product fallback requires attempting approved
  facts first; the controlled model returns empty before the distinct fallback.

The independent review corpus (682 ordinary / 144 personal / 240 abuse) is not
available locally. These authored regressions cover its supplied literals and do
not claim to reproduce the entire undisclosed corpus.

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
dialect, code switching and mixed clauses may miss the fallback veto or suppress a real
commerce fallback. ASCII `con ban` can also refer to a child; it intentionally
favours stock/sales. These uncertainties never reject safe comments at intake.
Safe unclassified comments reach the Director and attempt approved-fact answering;
only their missing-information fallback is forbidden. No corpus establishes universal coverage.

## Round 7 additions

- "shop/chi co <item> khong?" is a goods-availability question (commerce fallback allowed) unless it matches the narrow private-life patterns or a greeting about the host state (khoe/vui/buon...).
- In text that carries Vietnamese marks elsewhere, an unmarked "ngu" is the word itself (not a missing-tone "ngu"), except next to sleep context (di/som/ngon...); "ngu nhu bo/cho/lon/heo" is abuse.
