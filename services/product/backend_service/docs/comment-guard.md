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
cannot open a response turn, update topic cooldown or interrupt the script. Question
marks or commerce/question vocabulary preserve eligibility. Greetings with questions
remain eligible. Fallback for recognized social chat is refused at the speech boundary.
Real unknown questions retain the approved safe fallback.

Live-follow: `non_question` and `safety_filtered` use comment.skipped with event_ids,
final=true; Studio maps these to its existing skipped label. Events contain no text.
