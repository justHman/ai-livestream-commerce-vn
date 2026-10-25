# Comment guard

Curated toxicity and harassment resources retain their filenames and publish version 2
through provenance/resource_versions. No external dataset or dependency is loaded.

Add a pattern only after project review of its category, public-source provenance and
false positives. Add abuse and benign commerce regressions to test_comment_guard.py.
Run safety, ingestion and live-follow tests before activation. Never log rejected text;
use the existing fingerprint and reason codes.

Matching uses token/phrase boundaries, Unicode normalization, accent folding (d-bar to d),
basic leet, elongation reduction and bounded separated-letter joins. Original text is
unchanged. Accented benign homographs are protected; bare ASCII homographs remain
ambiguous. Short abbreviations match only complete tokens. This is curated coverage,
not a universal Vietnamese moderation classifier.

Social chat is skipped before reducer/director with final reason `non_question`, so it
cannot open a response turn, update topic cooldown or interrupt the script. A question
mark alone does not establish commerce intent. Greetings with shop questions remain
eligible. Fallback for recognized social chat is refused at the speech boundary.
Real shop questions retain the approved safe fallback.

Live-follow: `non_question` and `safety_filtered` use comment.skipped with event_ids,
final=true; Studio maps these to its existing skipped label. Events contain no text.

## Fallback eligibility and repetition

`answer_context.question_kind` requires positive shop/question signals or an explicit
personal question to the host. An unknown reducer topic is never evidence of commerce.
Unrelated, ambiguous or meaningless comments use final `not_answerable_non_commerce`
before reducer/Director; the approved-speech boundary checks again before generation.
Abusive inputs retain the safety refusal. Accent folding protects non-shop meanings
such as mưa/mua, máu/màu and gia đình/giá.

Missing approved answers use fixed text for the current product, a recognized different
product category, or a neutral question when uncertain. These texts never echo the
viewer or claim their question was detailed. Different goods and personal questions
never use current-product facts. Product-category recognition is curated and finite;
review the noun rules and add contrasting tests when extending supported merchandise.
`is_deflection` must recognize every fixed wording.

The Director checks the per-session `deflection_interval_sec` at dispatch, including
prefetched turns. Default is 90 seconds; `DIRECTOR_DEFLECTION_INTERVAL_SEC` overrides
it with a finite positive number. This covers personal deflections too. Suppressed
members use final `deflection_rate_limited`, remain skipped rather than answered,
and add no topic cooldown. A spoken cluster receives one shared response. Actual
approved answers remain eligible while the deflection interval is active.
