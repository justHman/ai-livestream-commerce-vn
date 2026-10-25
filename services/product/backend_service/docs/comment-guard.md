# Comment guard

Curated toxicity and harassment resources retain their filenames and publish version 3
through provenance/resource_versions. Unsafe-content remains at its existing version.
No external dataset or dependency is loaded.

Add a pattern only after project review of its category, public-source provenance and
false positives. Add abuse and benign commerce regressions to test_comment_guard.py.
Run safety, ingestion and live-follow tests before activation. Never log rejected text;
use the existing fingerprint and reason codes.

Matching uses token/phrase boundaries, Unicode normalization, accent folding (d-bar to d),
basic leet, elongation reduction and bounded separated-letter joins. Original text is
unchanged. Matching keeps the original abbreviation view (so `cc` and `đmm` survive)
and adds a view reducing double or longer repeated letters to one. Symbol joining
precedes this reduction: `v.v.c.l` becomes `vvcl`, then `vcl`. `địtt`, `nguu`,
`cặcc`, `địtttt`, `ngu uuu` and repeated profanity remain covered. Resource patterns
are not reduced from `cc` into a one-letter ban.

Original accents take precedence over folded views. Benign words such as ngủ/ngữ,
lớn, các, đủ/dư, buổi, đeo/dẻo, Bùi, đào/dạo, điện and hàm are protected. Ambiguous
ASCII tokens (`ngu`, `lon`, `cac`, `du`, `buoi`, `deo`, `bui`, `dien`, etc.) require
additional evidence such as an adjacent insult address (`đồ`, `thằng`/`thang`,
`mày`/`may`), `ngu quá`, an elongated insult or a bare `ngu`. ASCII `do ngu` alone
does not prove abuse: it can mean đồ ngủ. Neither `con`/còn nor a product question
alone is insult evidence. Thus `do ngu co size nao`, `size lon con khong`,
`cac mau nao`, `du size khong`, `buoi toi ship nhe`, `vai deo khong` and accented
equivalents pass safety. Names such as Bùi Thị Xuân and Bùi Viện also pass.

`điên` alone and `bùi` are removed from automatic bans; `đẹp điên`/`rẻ điên` pass,
while targeted `đồ điên`, `thằng điên`, `mày điên` still match. `dao kéo` is ordinary
merchandise; threat phrases `cầm dao`, `mang dao`, `chém chết` remain banned.
Unambiguous profanity and abbreviations (`đm`, `đmm`, standalone `dm`, `địt`,
`lồn`, `cặc`, `vcl`, `vkl`, standalone `cc`, `đéo`, `đ.m`, `v c l`) remain covered.
Short abbreviations match only complete tokens, preserving admin/account/acc.
This is curated coverage, not a universal Vietnamese moderation classifier.

Social chat is skipped before reducer/director with final reason `non_question`, so it
cannot open a response turn, update topic cooldown or interrupt the script. Exact
social responses such as `ok` remain social even with punctuation. Greetings mixed
with shop questions remain eligible. Fallback for recognized social chat is refused
at the speech boundary. Real shop questions retain the approved safe fallback.

Live-follow: `non_question` and `safety_filtered` use comment.skipped with event_ids,
final=true; Studio maps these to its existing skipped label. Events contain no text.

## Fallback eligibility and repetition

Admission and fallback authorization are separate decisions. Admission defaults to
allow after safety. `question_kind` returns `unclassified` when it cannot establish
commerce; ingestion still calls Coordinator/reducer with the original comment.
There is no rejection merely because a commerce keyword is absent. This includes
`admin ơi`, `acc`, `xin mã`, `giảm chút nhé`, `địa chỉ số điện thoại`,
`gửi Nguyễn Thị Lan`, `đẹp điên`, and their unaccented forms.

Only certain social shapes are skipped as `non_question`: complete laughter,
emoji/symbol/number-only text, acknowledgments, bare greetings, complete generic
compliments without an object, repeated characters and known keyboard runs.
Matching is against the whole comment; mixed product questions or requests pass.
Unknown Unicode letters are not treated as emoji or meaningless text.

Explicit personal questions to the host are checked before ambiguous commerce:
a host pronoun/address plus a personal predicate (spouse, hometown, age, eating,
drinking, sleeping, love, family, occupation, etc.) and interrogative evidence.
`chị có định lấy chồng không?` and `chị quê Cần Thơ à?` are silent, as are their
ASCII forms. Bare `lấy` and a city never establish commerce. A purchase use of
`lấy` requires a quantity/unit/product/code; a city requires a shipping/destination
verb. Concrete product evidence still preserves `chị mặc áo này size nào` and
`em dùng nước hoa gì`. Personal members dominate mixed clusters so no such member
reaches an LLM, fallback or TTS. Final `not_answerable_non_commerce` is recorded
without interruption or topic cooldown. Speech preparation checks this again.

A fixed missing-information fallback requires positive commerce evidence: product,
price, size, colour, stock, material, shipping, codes, discounts, payment, returns,
warranty or shop contact requests, or an intact approved product phrase. A generic
question mark/interrogative alone is insufficient. Known current/different goods
retain their distinct fixed texts and never borrow facts from a different product.
`unclassified` comments can use the normal validated answer path; when that path
has no approved answer, fallback authorization refuses speech. This later refusal
does not undo intake acceptance. The fallback never echoes viewer text.
`is_deflection` must recognize every fixed wording.

`chốt đơn` and `em lấy 2 cái` retain normal reducer/Director routing. New purchase
event IDs bypass topic cooldown; existing handled-ID suppression remains.

The Director checks the per-session `deflection_interval_sec` at dispatch, including
prefetched turns. Default is 90 seconds; `DIRECTOR_DEFLECTION_INTERVAL_SEC` overrides
it with a finite positive number. Personal deflections are no longer generated. Suppressed
members use final `deflection_rate_limited`, remain skipped rather than answered,
and add no topic cooldown. A spoken cluster receives one shared response. Actual
approved answers remain eligible while the deflection interval is active.

Round-two regression coverage in `tests/unit/test_comment_guard_round2.py` uses
more than 300 distinct comments, including at least 200 real sales/viewer comments,
30 social comments, 40 personal questions and 60 abusive comments. Accented and
unaccented examples run through authenticated ASGI/HTTP ingestion with the real
safety gate, Coordinator and reducer spies. Personal/social silence is also checked
at speech preparation, including mixed clusters; unclassified comments with no
approved answer cannot produce a missing-information fallback.

Ambiguous ASCII abuse homographs without insult evidence deliberately favour the
buyer (for example `cac`, `lon`, `do ngu`). This is curated coverage, not a universal
Vietnamese moderation or intent classifier. Personal/product ambiguity and dialects
remain finite-rule limitations; uncertainty in admission must keep passing onward.
