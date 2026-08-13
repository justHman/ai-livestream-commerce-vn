"""Task 11.1: bounded authoritative live script position (ScriptState).

Proves: exact script set/version/product/sentence identity is held; sentence
completion records the last-completed index and the exact next sentence;
the state holds a fixed size (one position — never grows with transcript);
render_context contains only bounded fields (no transcript).
"""

from __future__ import annotations

import pytest

from backend.application.live_runtime.script_state import ScriptState


def test_bind_holds_exact_identity() -> None:
    state = ScriptState()
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Chào bạn, hôm nay giới thiệu tai nghe P020.",
    )

    position = state.position
    assert position is not None
    assert position.script_set_id == "set-A"
    assert position.approved_version_id == "v3"
    assert position.product_id == "P020"
    assert position.sentence_index == 0
    assert position.last_completed_sentence_index is None
    assert position.next_sentence == "Chào bạn, hôm nay giới thiệu tai nghe P020."


def test_record_completion_advances_index_and_keeps_exact_next_sentence() -> None:
    state = ScriptState()
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Câu mở đầu.",
    )
    state.record_sentence_completed(
        completed_sentence="Câu mở đầu.", next_sentence="Câu tiếp theo."
    )

    position = state.position
    assert position is not None
    assert position.sentence_index == 1
    assert position.last_completed_sentence_index == 0
    assert position.next_sentence == "Câu tiếp theo."


def test_completion_without_bind_fails() -> None:
    state = ScriptState()
    with pytest.raises(RuntimeError):
        state.record_sentence_completed(completed_sentence="x", next_sentence="y")


def test_completion_of_non_current_sentence_fails() -> None:
    state = ScriptState()
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Câu mở đầu.",
    )
    with pytest.raises(RuntimeError):
        state.record_sentence_completed(completed_sentence="Sai câu.", next_sentence="y")


def test_rebind_resets_position() -> None:
    state = ScriptState()
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Câu mở đầu.",
    )
    state.record_sentence_completed(completed_sentence="Câu mở đầu.", next_sentence="Câu hai.")
    state.bind(
        script_set_id="set-B",
        approved_version_id="v1",
        product_id="P001",
        first_sentence="Câu mới.",
    )

    position = state.position
    assert position is not None
    assert (position.script_set_id, position.approved_version_id, position.product_id) == (
        "set-B",
        "v1",
        "P001",
    )
    assert position.sentence_index == 0
    assert position.last_completed_sentence_index is None
    assert state.pending_completion is None


def test_state_stays_one_position_over_many_completions() -> None:
    state = ScriptState()
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Câu 0.",
    )
    for index in range(100):
        completed = state.position.next_sentence if state.position is not None else "?"
        state.record_sentence_completed(
            completed_sentence=completed, next_sentence=f"Câu {index + 1}."
        )
        assert state.position is not None
        assert state.position.sentence_index == index + 1

    assert state.position.last_completed_sentence_index == 99


def test_render_context_is_bounded_and_free_of_transcript() -> None:
    state = ScriptState()
    transcript = [
        "khách xem 1: giá bao nhiêu?",
        "host: giá 1,2 triệu.",
        "khách xem 2: cái đó sạc nhanh không?",
    ]
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Giới thiệu P020.",
    )

    context = state.render_context()
    assert context["script_bound"] is True
    assert context["product_id"] == "P020"
    assert context["sentence_index"] == 0
    assert context["next_sentence"] == "Giới thiệu P020."
    assert "transcript" not in context
    for line in transcript:
        assert line not in str(context)


def test_render_context_before_bind_is_bounded() -> None:
    state = ScriptState()
    assert state.render_context() == {"script_bound": False}


def test_render_context_size_stays_small_regardless_of_stream() -> None:
    state = ScriptState()
    state.bind(
        script_set_id="set-A",
        approved_version_id="v3",
        product_id="P020",
        first_sentence="Câu 0.",
    )
    rendered_lengths = []
    for index in range(1_000):
        completed = state.position.next_sentence if state.position is not None else "?"
        state.record_sentence_completed(
            completed_sentence=completed,
            next_sentence=f"Chữ cái lặp lại {index % 10}",
        )
        rendered_lengths.append(len(str(state.render_context())))
    assert max(rendered_lengths) < 2_000
    assert len(rendered_lengths) == 1_000
