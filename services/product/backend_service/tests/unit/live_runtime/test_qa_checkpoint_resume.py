"""Task 14.9/14.11: Q&A checkpoint-resume proof with REAL cursor + arbiter.

Builds the REAL sentence map + script cursor over the exact P010 approved
text (tasks 13.1-13.3), plays 6 sentences normally (sentence 7 is the
current playing sentence), injects a high-score P020 candidate, and drives
the REAL ``SpeechArbiter`` boundary flow: sentence 7 completes normally
(non-preempted), the P020 Q&A speaks, the resume bridge names the script
product, and the arbiter resumes at EXACT sentence 8 - no repeat of
sentence 7, no skip of sentence 8.
"""

from __future__ import annotations

import pytest

from backend.application.live_runtime.sentence_speaker import (
    ScriptSentencePlayer,
)
from tests.unit.live_runtime.qa_fixtures import (
    P010_SENTENCES,
    P020_QA_ENVELOPE,
    RecordingQaResolver,
    RecordingSpeechFake,
    build_p020_answer,
    build_pending_qa_store,
    build_script_cursor,
)

SESSION_ID = "session-qa-checkpoint-resume"


def test_modules_present_for_real_flow() -> None:
    pytest.importorskip(
        "backend.application.live_runtime.script_cursor",
        reason="script_cursor must land before the real-cursor scenarios run",
    )


async def test_checkpoint_is_exact_sentence_eight_after_seven() -> None:
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
    assert cursor.checkpoint_next_before_qa() == P010_SENTENCES[7]
    assert cursor.current_sentence().text == P010_SENTENCES[7]


async def test_qna_boundary_preserves_checkpoint_and_resumes_exact_sentence() -> None:
    pytest.importorskip(
        "backend.application.live_runtime.speech_arbiter",
        reason="speech_arbiter lands in parallel C13 task 14.1",
    )
    from backend.application.live_runtime.speech_arbiter import ArbiterConfig, SpeechArbiter

    cursor = build_script_cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    # Play sentences 0..5; sentence 7 (index 6) is the playing sentence when
    # the high-priority P020 cluster becomes pending.
    for _ in P010_SENTENCES[:6]:
        await player.play_current(SESSION_ID)
    assert cursor.current_sentence().text == P010_SENTENCES[6]

    pending = build_pending_qa_store(P020_QA_ENVELOPE)
    arbiter = SpeechArbiter(
        cursor=cursor,
        player=player,
        pending=pending,
        resolver=RecordingQaResolver(outcome="answer"),
        speech=speech,
        config=ArbiterConfig(bridge_topic="P010"),
    )

    # Tick 1: sentence 7 completes normally (non-preempted), boundary sees the
    # winner -> QNA_PENDING. The checkpoint is now exact sentence 8.
    await arbiter.on_tick(SESSION_ID)
    assert cursor.sentence_index == 7
    assert cursor.checkpoint_next_before_qa() == P010_SENTENCES[7]

    # Ticks 2-5: QNA_PREPARING -> QNA_PLAYING (Q&A speaks) -> RESUME_BRIDGE
    # (the bridge branch executes on its tick).
    await arbiter.on_tick(SESSION_ID)
    await arbiter.on_tick(SESSION_ID)
    await arbiter.on_tick(SESSION_ID)
    await arbiter.on_tick(SESSION_ID)

    # Checkpoint preserved through the Q&A turn: exact sentence 8 still next.
    assert cursor.checkpoint_next_before_qa() == P010_SENTENCES[7]

    spoken = [text for _, text in speech.spoken]
    assert build_p020_answer() in spoken
    bridge = next((text for text in spoken if "tiếp tục với P010" in text), "")
    assert bridge != ""
    assert "P010" in bridge
    # Sentence 7 (index 6) was spoken exactly once - never repeated.
    assert spoken.count(P010_SENTENCES[6]) == 1

    # Ticks 5-6: resume at EXACT sentence 8, then the script finishes.
    await arbiter.on_tick(SESSION_ID)
    await arbiter.on_tick(SESSION_ID)

    spoken = [text for _, text in speech.spoken]
    assert spoken[-1] == P010_SENTENCES[7]
    # No skip: sentence 8 spoken exactly once, right after the bridge.
    bridge_idx = next(i for i, text in enumerate(spoken) if "tiếp tục với P010" in text)
    assert spoken[bridge_idx + 1] == P010_SENTENCES[7]
    assert spoken.count(P010_SENTENCES[7]) == 1
    assert cursor.finished is True
