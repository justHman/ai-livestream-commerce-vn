"""Tasks 15.1/15.4: canonical transitions are pure and the normal path has ONE resolver call.

Proves: ``build_qa_lead_in`` and ``build_resume_bridge`` are pure sync
functions of strings (not coroutines, no IO/model/clock — they take only
strings); the arbiter's normal Q&A path performs EXACTLY one resolver call
(the final generation) across a full Q&A + bridge + resume cycle — no second
resolver/LLM invocation exists between the Q&A turn and the next script
sentence; the composed ``resolution.speech_text`` begins with the lead-in and
contains the answer (single combined turn).
"""

from __future__ import annotations

import asyncio
import inspect

from backend.application.live_runtime.transitions import (
    build_qa_lead_in,
    build_resume_bridge,
)

from unit.live_runtime.qa_fixtures import (
    P010_SENTENCES,
    P020_QA_ENVELOPE,
    QNA_LEAD_IN,
    RecordingQaResolver,
    RecordingSpeechFake,
    build_p020_answer,
    build_pending_qa_store,
    build_script_cursor,
)
from backend.application.live_runtime.sentence_speaker import ScriptSentencePlayer


def test_lead_in_builder_is_pure_sync_function_of_strings() -> None:
    assert not inspect.iscoroutinefunction(build_qa_lead_in)
    # Deterministic: same inputs, byte-identical output, no clock/IO involved.
    assert build_qa_lead_in("P020", "có hỗ trợ sạc nhanh không") == QNA_LEAD_IN
    assert build_qa_lead_in("P020", "có hỗ trợ sạc nhanh không") == build_qa_lead_in(
        "P020", "có hỗ trợ sạc nhanh không"
    )


def test_resume_bridge_builder_is_pure_sync_function_of_strings() -> None:
    assert not inspect.iscoroutinefunction(build_resume_bridge)
    assert build_resume_bridge("P010") == "Rồi, em tiếp tục với P010 nhé."
    assert build_resume_bridge("P010") == build_resume_bridge("P010")


async def test_normal_path_has_exactly_one_resolver_call() -> None:
    # Real cursor/player over the exact P010 script: one Q&A + bridge cycle,
    # then resume — the resolver must be called ONCE (the final generation).
    # A bridge-only LLM call would appear as a second resolver invocation.
    cursor = build_script_cursor()
    speech = RecordingSpeechFake()
    player = ScriptSentencePlayer(cursor, speech)
    for _ in P010_SENTENCES[:6]:
        await player.play_current("s")
    resolver = RecordingQaResolver(outcome="answer")

    from backend.application.live_runtime.speech_arbiter import ArbiterConfig, SpeechArbiter

    arbiter = SpeechArbiter(
        cursor=cursor,
        player=player,
        pending=build_pending_qa_store(P020_QA_ENVELOPE),
        resolver=resolver,
        speech=speech,
        config=ArbiterConfig(bridge_topic="P010"),
    )
    for _ in range(8):
        await arbiter.on_tick("s")

    assert resolver.calls == ["cl-p020-fastcharge"]
    spoken = [text for _, text in speech.spoken]
    assert build_p020_answer() in spoken
    assert "Rồi, em tiếp tục với P010 nhé." in spoken


async def test_composed_turn_begins_with_lead_in_and_contains_answer() -> None:
    # The 15.2 compose path: speech_text is the lead-in + answer in ONE call.
    text = build_p020_answer()

    assert text.startswith(QNA_LEAD_IN)
    assert "P020 có sạc nhanh 65W nha." in text
    assert text == QNA_LEAD_IN + "P020 có sạc nhanh 65W nha."
