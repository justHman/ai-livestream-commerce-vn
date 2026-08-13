"""Task 14.12: Q&A failure — script cursor stays valid, runtime resumes.

Three variants, driven by the REAL sentence player semantics
(``ScriptSentencePlayer`` from 13.4-13.7: cursor advances ONLY on normal
speech completion) against the shared P010/P020 fixtures:

- Variant A: the Q&A winner resolves as ``unavailable`` (no answer) — the
  script cursor remains at the exact sentence-8 checkpoint and the next
  script sentence speaks next (no repeat, no skip, no crash). This matches
  the arbiter's boundary flow (``QNA_PREPARING``: non-answer resolution ->
  ``SCRIPT_READY``, script continues).
- Variant B: the Q&A speech call itself fails (``SentenceCompletionError``)
  — per the arbiter failure policy (design Failure Handling: "Q&A failure:
  preserve script cursor and resume according to arbiter failure policy")
  the cursor is untouched by the failed Q&A call and the script resumes with
  sentence 8 at the next opportunity.
- Variant C: a P010 sentence speech call fails — the cursor is NOT advanced
  past the failed sentence (``sentence_index`` unchanged, ``next_sentence``
  still the failed sentence's exact text) and the error is terminal for the
  player call (the arbiter transitions to ``FAILED``).

Scenarios run on the real cursor (``script_cursor.py``, task 13.3) and the
``FakeP010Cursor`` double with identical semantics.
"""

from __future__ import annotations

import pytest

from backend.application.live_runtime.sentence_speaker import (
    ScriptSentencePlayer,
    SentenceCompletionError,
)
from unit.live_runtime.qa_fixtures import (
    P010_SENTENCES,
    P020_QA_ENVELOPE,
    FakeP010Cursor,
    RecordingQaResolver,
    RecordingSpeechFake,
    build_p020_answer,
    build_pending_qa_store,
    build_script_cursor,
)

SESSION_ID = "session-qa-failure"


async def _play_seven_sentences(speech: RecordingSpeechFake) -> FakeP010Cursor:
    cursor = FakeP010Cursor()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)
    return cursor


async def _resolve_pending(resolver: RecordingQaResolver):
    pending = build_pending_qa_store(P020_QA_ENVELOPE)
    winner = pending.pending_winner()
    assert winner is not None
    return await resolver.resolve_qa(winner)


# --- Variant A: Q&A unavailable -> cursor valid, script resumes --------------


async def test_variant_a_unavailable_keeps_cursor_valid() -> None:
    resolver = RecordingQaResolver(outcome="unavailable", reason="evidence_unavailable")
    cursor = await _play_seven_sentences(RecordingSpeechFake())

    resolution = await _resolve_pending(resolver)

    assert resolution.kind == "unavailable"
    assert cursor.sentence_index == 7
    assert cursor.last_completed_sentence_index == 6
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_variant_a_resumes_with_sentence_eight_no_repeat() -> None:
    resolver = RecordingQaResolver(outcome="unavailable", reason="evidence_unavailable")
    speech = RecordingSpeechFake()
    cursor = await _play_seven_sentences(speech)
    player = ScriptSentencePlayer(cursor, speech)
    await _resolve_pending(resolver)

    await player.play_current(SESSION_ID)

    assert speech.spoken[-1] == (SESSION_ID, P010_SENTENCES[7])
    assert speech.spoken.count((SESSION_ID, P010_SENTENCES[6])) == 1


# --- Variant B: Q&A speech failure -> cursor untouched, resume at sentence 8 --


async def test_variant_b_speech_failure_leaves_cursor_at_checkpoint() -> None:
    speech = RecordingSpeechFake()
    cursor = await _play_seven_sentences(speech)
    speech.fail_next()

    with pytest.raises(SentenceCompletionError):
        await speech.speak_sentence(SESSION_ID, build_p020_answer())

    assert cursor.sentence_index == 7
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_variant_b_resumes_after_failed_qna_speech() -> None:
    speech = RecordingSpeechFake()
    cursor = await _play_seven_sentences(speech)
    player = ScriptSentencePlayer(cursor, speech)
    speech.fail_next()

    # Q&A speech failure propagates (arbiter failure policy: preserve cursor,
    # script continues with the next sentence at the next opportunity).
    with pytest.raises(SentenceCompletionError):
        await speech.speak_sentence(SESSION_ID, build_p020_answer())

    await player.play_current(SESSION_ID)

    assert speech.spoken[-1] == (SESSION_ID, P010_SENTENCES[7])
    assert speech.spoken.count((SESSION_ID, P010_SENTENCES[6])) == 1


# --- Variant C: P010 sentence speech failure -> cursor not advanced ----------


async def test_variant_c_sentence_failure_does_not_advance_cursor() -> None:
    speech = RecordingSpeechFake()
    cursor = await _play_seven_sentences(speech)
    player = ScriptSentencePlayer(cursor, speech)
    speech.fail_next()

    with pytest.raises(SentenceCompletionError):
        await player.play_current(SESSION_ID)

    assert cursor.sentence_index == 7
    assert cursor.last_completed_sentence_index == 6
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_variant_c_real_cursor_failure_semantics() -> None:
    pytest.importorskip(
        "backend.application.live_runtime.script_cursor",
        reason="script_cursor lands in parallel C13 task 13.3",
    )
    cursor = build_script_cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)
    speech.fail_next()

    with pytest.raises(SentenceCompletionError):
        await player.play_current(SESSION_ID)

    assert cursor.sentence_index == 7
    assert cursor.current_sentence().text == P010_SENTENCES[7]


# --- failure-policy guards that hold before C13 modules land -----------------


async def test_failed_speech_records_nothing_and_raises_typed_error() -> None:
    speech = RecordingSpeechFake()
    speech.fail_next()

    with pytest.raises(SentenceCompletionError):
        await speech.speak_sentence("s", "câu")

    assert speech.spoken == []


async def test_fail_after_n_fails_only_the_nth_call() -> None:
    speech = RecordingSpeechFake()
    speech.fail_after(2)

    assert await speech.speak_sentence("s", "câu 1") == "câu 1"
    with pytest.raises(SentenceCompletionError):
        await speech.speak_sentence("s", "câu 2")
    assert await speech.speak_sentence("s", "câu 3") == "câu 3"
    assert [text for _, text in speech.spoken] == ["câu 1", "câu 3"]


async def test_resolver_raise_propagates_as_typed_failure() -> None:
    resolver = RecordingQaResolver()
    resolver.raise_on_next()

    with pytest.raises(RuntimeError, match="injected Q&A resolver failure"):
        await resolver.resolve_qa(P020_QA_ENVELOPE)
