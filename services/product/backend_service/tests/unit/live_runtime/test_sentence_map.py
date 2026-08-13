"""Task 13.1/13.2: deterministic sentence-map derivation and exact-artifact proof.

Proves the map derives exact contiguous slices from the approved
``spoken_text``: mixed terminators split at the shared Change A set, decimal
prices / URLs / emails / ``đ`` suffixes never split inside, and
``map.concat()`` reproduces ``spoken_text`` byte-for-byte.
"""

from __future__ import annotations

from backend.application.live_runtime.sentence_map import (
    SentenceMap,
    derive_sentence_map,
    map_from_binding,
)
from backend.application.script_authoring.runtime_handoff import ResolvedApprovedScript

SCRIPT_SET_ID = "set-A"
APPROVED_VERSION_ID = "v3"
PRODUCT_ID = "P020"


def _map(spoken_text: str) -> SentenceMap:
    return derive_sentence_map(
        script_set_id=SCRIPT_SET_ID,
        approved_version_id=APPROVED_VERSION_ID,
        product_id=PRODUCT_ID,
        spoken_text=spoken_text,
    )


# ---------- 13.1: deterministic derivation ----------


def test_splits_at_mixed_terminators() -> None:
    sentence_map = _map("Câu 1. Câu 2? Câu 3! Câu 4…")

    assert [span.text for span in sentence_map.spans] == [
        "Câu 1. ",
        "Câu 2? ",
        "Câu 3! ",
        "Câu 4…",
    ]


def test_span_offsets_are_exact_slices() -> None:
    spoken_text = "Câu 1. Câu 2?"
    sentence_map = _map(spoken_text)

    for span in sentence_map.spans:
        assert span.text == spoken_text[span.start : span.end]
        assert span.text != ""


def test_spans_are_contiguous_and_non_overlapping() -> None:
    sentence_map = _map("Câu 1. Câu 2? Câu 3!")

    assert sentence_map.spans[0].end == sentence_map.spans[1].start
    assert sentence_map.spans[1].end == sentence_map.spans[2].start


def test_decimal_price_does_not_split_at_dot() -> None:
    sentence_map = _map("P020 giá 1.2 triệu đồng, mua ngay nhé!")

    assert [span.text for span in sentence_map.spans] == [
        "P020 giá 1.2 triệu đồng, mua ngay nhé!",
    ]


def test_url_does_not_split_at_dots() -> None:
    sentence_map = _map("Xem chi tiết tại https://shop.example.com/p020. Giá tốt!")

    assert sentence_map.spans[0].text == "Xem chi tiết tại https://shop.example.com/p020. "
    assert sentence_map.spans[1].text == "Giá tốt!"


def test_email_does_not_split_at_dots() -> None:
    sentence_map = _map("Liên hệ hotro@shop.example.com nhé. Cảm ơn!")

    assert sentence_map.spans[0].text == "Liên hệ hotro@shop.example.com nhé. "
    assert sentence_map.spans[1].text == "Cảm ơn!"


def test_currency_suffix_does_not_split() -> None:
    sentence_map = _map("Giá chỉ 199.000đ, giảm thêm 50%!")

    assert [span.text for span in sentence_map.spans] == ["Giá chỉ 199.000đ, giảm thêm 50%!"]


def test_whitespace_between_sentences_belongs_to_no_span() -> None:
    sentence_map = _map("Câu 1.   \n  Câu 2.")

    assert sentence_map.spans[0].text == "Câu 1.   \n  "
    assert sentence_map.spans[1].start == sentence_map.spans[0].end
    assert sentence_map.spans[1].text == "Câu 2."


def test_leading_whitespace_is_not_part_of_a_span() -> None:
    sentence_map = _map("  Câu 1. Câu 2.")

    assert sentence_map.spans[0].text == "Câu 1. "
    assert sentence_map.spans[0].start == 2


def test_empty_sentences_never_emitted() -> None:
    sentence_map = _map("Câu 1... Câu 2!")

    assert all(span.text != "" for span in sentence_map.spans)
    assert sentence_map.spans[0].text == "Câu 1... "
    assert sentence_map.spans[1].text == "Câu 2!"


def test_map_from_binding_wraps_resolved_script() -> None:
    script = ResolvedApprovedScript(
        product_id="P001", approved_version_id="v-1", spoken_text="Câu một. Câu hai?"
    )

    sentence_map = map_from_binding("set-1", script)

    assert sentence_map.script_set_id == "set-1"
    assert sentence_map.approved_version_id == "v-1"
    assert sentence_map.product_id == "P001"
    assert len(sentence_map) == 2


# ---------- 13.2: concatenation reproduces the exact artifact ----------


def test_concat_reproduces_spoken_text_byte_for_byte() -> None:
    spoken_text = (
        "Câu 1. Câu 2? Câu 3! Câu 4… "
        "P020 giá 1.2 triệu đồng, xem tại https://shop.example.com/p020. "
        "Email hotro@shop.example.com, giảm 50%! Mua ngay nhé."
    )
    sentence_map = _map(spoken_text)

    assert sentence_map.concat() == spoken_text


def test_concat_preserves_internal_whitespace() -> None:
    spoken_text = "Câu 1.   Câu 2.  Câu 3."
    sentence_map = _map(spoken_text)

    assert sentence_map.concat() == spoken_text


def test_map_is_derivative_not_new_version() -> None:
    spoken_text = "Câu 1. Câu 2."
    sentence_map = _map(spoken_text)

    assert sentence_map.spoken_text == spoken_text
    assert sentence_map.approved_version_id == APPROVED_VERSION_ID
