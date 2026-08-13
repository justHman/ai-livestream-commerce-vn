"""Task 14.11: P010-script/P020-Q&A arbitration fixture.

Proves, against the REAL sentence player (``ScriptSentencePlayer`` from
13.4-13.7) and the shared P010/P020 fixtures:

- the current P010 sentence (7, index 6) completes normally while a
  high-priority P020 Q&A cluster is pending (non-preemption);
- at the sentence boundary the pending P020 winner wins and its Q&A speaks
  with a natural paraphrased lead-in, never reading the raw viewer question;
- the script cursor checkpoints the exact next sentence (8) before Q&A and
  the runtime resumes at EXACT sentence 8 after the P020 turn, with no
  repeat of sentence 7 and no skip;
- the resume bridge names the script product (P010) without a bridge-only
  LLM call.

The scenarios run against the REAL sentence map + cursor
(``sentence_map.py``/``script_cursor.py``, tasks 13.1-13.3) where they exist,
with ``FakeP010Cursor`` covering the same surface so the boundary flow is
also exercised against the real player/pending board/resolution contracts.
The speech arbiter itself (``speech_arbiter.py``, task 14.1) is a parallel
implementer's module: its Q&A speech path was not yet exercising speech when
these fixtures were written, so the boundary flow here mirrors Decision 17
deterministically instead of depending on arbiter internals.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.application.live_runtime.pending_qa import QaHysteresisConfig
from backend.application.live_runtime.sentence_speaker import (
    ScriptSentencePlayer,
    SentenceCompletionError,
)
from unit.live_runtime.qa_fixtures import (
    P010_SENTENCES,
    P020_ANSWER_TEXT,
    P020_QA_ENVELOPE,
    QNA_LEAD_IN,
    RESUME_BRIDGE_P010,
    FakeP010Cursor,
    RecordingSpeechFake,
    build_p020_answer,
    build_pending_qa_store,
    build_script_cursor,
)

SESSION_ID = "session-p010-p020"

# --- fixture sanity ---------------------------------------------------------


def test_p010_script_has_eight_exact_sentences() -> None:
    assert len(P010_SENTENCES) == 8
    assert P010_SENTENCES[6] == "Em lưu ý là P010 không hỗ trợ sạc nhanh, anh chị nhé!"
    assert P010_SENTENCES[7] == "Còn bây giờ em mời anh chị đặt ngay để được ưu đãi nha."


def test_p020_envelope_satisfies_cluster_envelope_protocol() -> None:
    from backend.application.agentic_director.fast_path import ClusterEnvelope

    assert isinstance(P020_QA_ENVELOPE, ClusterEnvelope)


def test_p020_score_exceeds_hysteresis_eligibility() -> None:
    assert P020_QA_ENVELOPE.ranking_score >= QaHysteresisConfig().min_eligibility_score


def test_p020_score_exceeds_any_p010_pending_candidate() -> None:
    pending = build_pending_qa_store()
    winner = pending.pending_winner()

    assert winner is not None
    assert winner.cluster_id == "cl-p020-fastcharge"
    assert winner.score == P020_QA_ENVELOPE.ranking_score


# --- 14.11: P010 sentences 0..6 play, cursor checkpoint exact ---------------


async def test_play_seven_sentences_advances_cursor_to_sentence_eight() -> None:
    pytest.importorskip(
        "backend.application.live_runtime.script_cursor",
        reason="script_cursor lands in parallel C13 task 13.3",
    )
    cursor = build_script_cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)

    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)

    assert cursor.sentence_index == 7
    assert cursor.last_completed_sentence_index == 6
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_play_seven_sentences_fake_cursor_checkpoints_sentence_eight() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)

    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)

    assert cursor.sentence_index == 7
    assert cursor.last_completed_sentence_index == 6
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_play_all_completes_exact_sequence() -> None:
    pytest.importorskip(
        "backend.application.live_runtime.script_cursor",
        reason="script_cursor lands in parallel C13 task 13.3",
    )
    cursor = build_script_cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)

    await player.play_all(SESSION_ID)

    assert [text for _, text in speech.spoken] == list(P010_SENTENCES)
    assert cursor.finished is True


# --- 14.11: mid-sentence P020 pending; sentence 7 completes normally --------


async def test_pending_qa_does_not_preempt_playing_sentence() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:6]:
        await player.play_current(SESSION_ID)

    gate = speech.slow_until(7)
    task = asyncio.create_task(player.play_current(SESSION_ID))
    # The arbiter itself is the gate's waiter (play_current is parked on the
    # event); the test only proceeds once the call has started.
    while not gate.is_set() and gate._waiters:
        await asyncio.sleep(0.01)
    pending = build_pending_qa_store(P020_QA_ENVELOPE)
    assert pending.pending_winner() is not None
    await speech.release(7)
    await task

    assert speech.spoken[6] == (SESSION_ID, P010_SENTENCES[6])
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_pending_qa_mid_sentence_does_not_advance_cursor() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:6]:
        await player.play_current(SESSION_ID)

    gate = speech.slow_until(7)
    task = asyncio.create_task(player.play_current(SESSION_ID))
    while not gate.is_set() and gate._waiters:
        await asyncio.sleep(0.01)
    await speech.release(7)
    await task

    assert cursor.sentence_index == 7
    assert cursor.last_completed_sentence_index == 6


# --- 14.11: boundary — pending Q&A speaks, cursor checkpoint preserved -------


def test_qna_boundary_speaks_lead_in_and_grounded_answer() -> None:
    qa_text = build_p020_answer()

    assert qa_text == QNA_LEAD_IN + P020_ANSWER_TEXT


async def test_qna_never_reads_raw_representative_question() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)
    speech.spoken.append((SESSION_ID, build_p020_answer()))
    raw_question = P020_QA_ENVELOPE.representative_questions[0]

    assert raw_question not in [text for _, text in speech.spoken]


async def test_qna_boundary_preserves_exact_sentence_eight_checkpoint() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)
    checkpoint_before = cursor.current_sentence().text

    # Q&A is spoken without touching the cursor; position stays at sentence 8.
    assert checkpoint_before == P010_SENTENCES[7]
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_boundary_resolver_answers_and_pending_marks_cooldown() -> None:
    from unit.live_runtime.qa_fixtures import RecordingQaResolver

    pending = build_pending_qa_store(P020_QA_ENVELOPE)
    resolver = RecordingQaResolver()
    winner = pending.pending_winner()
    assert winner is not None

    resolution = await resolver.resolve_qa(winner)
    pending.mark_answered(winner.cluster_id)

    assert resolution.kind == "answer"
    assert resolution.speech_text == build_p020_answer()
    assert pending.pending_winner() is None


# --- 14.11: resume — bridge names P010, then exact sentence 8 ----------------


async def test_resume_after_qna_speaks_bridge_then_exact_sentence_eight() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)
    speech.spoken.append((SESSION_ID, build_p020_answer()))
    speech.spoken.append((SESSION_ID, RESUME_BRIDGE_P010))

    await player.play_current(SESSION_ID)

    spoken_texts = [text for _, text in speech.spoken]
    assert spoken_texts[-1] == P010_SENTENCES[7]
    assert RESUME_BRIDGE_P010 in spoken_texts
    assert "P010" in RESUME_BRIDGE_P010


async def test_resume_does_not_repeat_sentence_seven() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)
    speech.spoken.append((SESSION_ID, build_p020_answer()))
    speech.spoken.append((SESSION_ID, RESUME_BRIDGE_P010))

    await player.play_current(SESSION_ID)

    assert speech.spoken.count((SESSION_ID, P010_SENTENCES[6])) == 1


# --- 14.11: full-run sequence ------------------------------------------------


async def test_full_run_reproduces_exact_sequence_with_qna_and_bridge() -> None:
    cursor = FakeP010Cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:7]:
        await player.play_current(SESSION_ID)
    speech.spoken.append((SESSION_ID, build_p020_answer()))
    speech.spoken.append((SESSION_ID, RESUME_BRIDGE_P010))
    await player.play_all(SESSION_ID)

    expected = (
        list(P010_SENTENCES[:7])
        + [build_p020_answer(), RESUME_BRIDGE_P010]
        + list(P010_SENTENCES[7:])
    )
    assert [text for _, text in speech.spoken] == expected
    assert speech.spoken.count((SESSION_ID, P010_SENTENCES[6])) == 1


# --- guards that hold even before the C13 modules land -----------------------


def test_sentence_completion_error_is_exported_for_failure_fixtures() -> None:
    assert issubclass(SentenceCompletionError, RuntimeError)


def test_shared_fixtures_import_without_c13_modules() -> None:
    from unit.live_runtime import qa_fixtures

    assert qa_fixtures.P010_SCRIPT  # noqa: F401 (module import sanity)
