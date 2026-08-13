"""Task 13.3: bounded script cursor persistence over the C11 ScriptState.

Proves the cursor stores script set id / approved version id / product id /
current sentence index / last completed index / exact next sentence, and
that completing sentences advances the position exactly — never mutating
the approved artifact or the sentence map.
"""

from __future__ import annotations

import pytest

from backend.application.live_runtime.sentence_map import derive_sentence_map
from backend.application.live_runtime.script_cursor import ScriptCursor

SCRIPT_TEXT = "Câu 1. Câu 2? Câu 3!"


def _cursor() -> ScriptCursor:
    sentence_map = derive_sentence_map(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        spoken_text=SCRIPT_TEXT,
    )
    return ScriptCursor(sentence_map)


def test_bind_stores_identity_fields() -> None:
    cursor = _cursor()

    position = cursor.position()
    assert position.script_set_id == "set-A"
    assert position.approved_version_id == "v3"
    assert position.product_id == "P020"
    assert position.sentence_index == 0
    assert position.last_completed_sentence_index is None
    assert position.next_sentence == "Câu 1. "


def test_current_sentence_returns_exact_span() -> None:
    cursor = _cursor()

    span = cursor.current_sentence()

    assert span.index == 0
    assert span.text == "Câu 1. "
    assert span.text == SCRIPT_TEXT[span.start : span.end]


def test_complete_current_advances_to_exact_next_sentence() -> None:
    cursor = _cursor()
    cursor.complete_current()

    position = cursor.position()
    assert position.sentence_index == 1
    assert position.last_completed_sentence_index == 0
    assert position.next_sentence == "Câu 2? "


def test_complete_through_script_marks_finished() -> None:
    cursor = _cursor()
    cursor.complete_current()
    cursor.complete_current()
    cursor.complete_current()

    assert cursor.finished is True
    assert cursor.position().last_completed_sentence_index == 2
    assert cursor.position().next_sentence == ""


def test_finished_false_while_sentences_remain() -> None:
    cursor = _cursor()
    cursor.complete_current()

    assert cursor.finished is False


def test_next_sentence_returns_exact_text_until_end() -> None:
    cursor = _cursor()
    assert cursor.next_sentence() == "Câu 2? "

    cursor.complete_current()
    assert cursor.next_sentence() == "Câu 3!"

    cursor.complete_current()
    assert cursor.next_sentence() is None


def test_checkpoint_next_before_qa_does_not_advance() -> None:
    cursor = _cursor()

    checkpoint = cursor.checkpoint_next_before_qa()

    assert checkpoint == "Câu 1. "
    assert cursor.position().sentence_index == 0


def test_position_updates_are_exact_after_completion_sequence() -> None:
    cursor = _cursor()
    cursor.complete_current()
    cursor.complete_current()

    position = cursor.position()
    assert position.sentence_index == 2
    assert position.last_completed_sentence_index == 1
    assert position.next_sentence == "Câu 3!"


def test_render_context_passthrough_is_bounded() -> None:
    cursor = _cursor()

    context = cursor.render_context()

    assert context["script_bound"] is True
    assert context["product_id"] == "P020"
    assert context["sentence_index"] == 0
    assert context["next_sentence"] == "Câu 1. "


def test_completing_all_never_mutates_map_or_artifact() -> None:
    sentence_map = derive_sentence_map(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        spoken_text=SCRIPT_TEXT,
    )
    before_map = sentence_map
    before_text = sentence_map.spoken_text
    cursor = ScriptCursor(sentence_map)
    cursor.complete_current()
    cursor.complete_current()
    cursor.complete_current()

    assert sentence_map == before_map
    assert sentence_map.spoken_text == before_text
    assert sentence_map.concat() == SCRIPT_TEXT


def test_complete_current_after_finish_raises() -> None:
    cursor = _cursor()
    cursor.complete_current()
    cursor.complete_current()
    cursor.complete_current()

    with pytest.raises(RuntimeError):
        cursor.complete_current()
