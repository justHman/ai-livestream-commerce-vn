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

`answer_context.question_kind` favours the buyer when uncertain. Any question mark
(including fullwidth), interrogative (`không`, `chưa`, `nào`, `gì`, `sao`, `bao nhiêu`,
`mấy`, `đâu`, `ai`, `khi nào`, `bao giờ`), purchase request (`chốt`, `lấy`, `mua`, `đặt`,
`order`, `ib`, `inbox`, `giữ`, `để`, `cho em/mình`), product vocabulary (including
`nguyên liệu`, `kích thước`, `co giãn`, `mặc`, `dùng`, `loại`, `nước hoa`), known
destination or quantity plus unit keeps the comment eligible. Both accented and
ASCII forms work. An intact approved product phrase is also evidence; incidental
overlap with a fact and an unknown reducer topic are never evidence on their own.
`chốt đơn` and `em lấy 2 cái` reach the normal reducer/Director flow. `chot`/`lay`
are routed as purchase intents too; new purchase event IDs bypass topic cooldown
even when the representative text repeats. Existing handled-ID suppression remains.

Explicit personal questions to the host without product evidence are silent, even
with `?` or generic `gì`/`sao`: pronoun/host address plus eating, drinking, sleeping,
love, age, hometown, spouse/family, etc. uses final `not_answerable_non_commerce`
before reducer/Director. This appears in Studio as “Không trả lời” with the reason;
no model request, shop referral, TTS, interruption or topic cooldown is produced.
The approved-speech boundary refuses personal fallbacks again. Personal pronouns
with real product evidence (`chị mặc áo này size nào`, `em dùng nước hoa gì`) stay
eligible. Explicit unrelated topics (weather, geography, etc.) and meaningless text
without shopping/question evidence also remain silent. Accent folding protects
mưa/mua, máu/màu, gia đình/giá, and the discourse particle vậy/vay.

Missing approved answers use fixed text for the current product, a recognized different
product category, or a neutral question when uncertain. These texts never echo the
viewer or claim their question was detailed. Different goods never use current-product
facts; personal questions receive no speech. Product-category recognition is curated and finite;
review the noun rules and add contrasting tests when extending supported merchandise.
`is_deflection` must recognize every fixed wording.

The Director checks the per-session `deflection_interval_sec` at dispatch, including
prefetched turns. Default is 90 seconds; `DIRECTOR_DEFLECTION_INTERVAL_SEC` overrides
it with a finite positive number. Personal deflections are no longer generated. Suppressed
members use final `deflection_rate_limited`, remain skipped rather than answered,
and add no topic cooldown. A spoken cluster receives one shared response. Actual
approved answers remain eligible while the deflection interval is active.

Regression coverage in `tests/unit/test_comment_guard_review.py` includes over 80
benign sales comments and over 40 real purchase/question examples, both with and
without accents; authenticated ASGI intake, reducer reachability, repeated-letter
abuse, purchase cooldown and silence at both intake and speech boundaries are checked.
Ambiguous unaccented abuse may deliberately pass safety: reducing buyer false
positives takes precedence. Comments with no commerce/question evidence remain silent
at the subsequent eligibility gate. This finite vocabulary cannot resolve every
Vietnamese name, dialect, product category or personal/product ambiguity.
