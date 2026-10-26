from dataclasses import replace

import pytest

from backend.application.script_authoring.generation.script_variety import (
    VarietyPart,
    audit,
    improves,
    repair_budget,
    repair_candidates,
    opening_only,
)


def part(key, text, role="highlight", **kwargs):
    return VarietyPart(key, key.split(":")[0], role, text, **kwargs)


def test_global_three_token_openers_and_role_threshold_are_independent():
    parts = [
        part("a:1", "Mọi người ơi, hãy xem chất liệu.", "intro"),
        part("b:2", "Mọi người ơi, hãy xem màu sắc.", "highlight"),
        part("c:3", "Mọi người ơi, hãy xem cách giặt.", "assurance"),
    ]
    result = audit(parts)
    assert result.excess == 2  # independent part and sentence counters, each excess one
    assert len(result.groups) == 2
    assert all(g.role is None for g in result.groups)
    assert audit(parts[:2]).excess == 0
    same_role = [replace(p, role="highlight") for p in parts[:2]]
    assert audit(same_role).excess == 2


def test_unicode_normalization_preserves_vietnamese_accents():
    parts = [part("a", "Mình nói về chất liệu."), part("b", "Mi\u0300nh NÓI VỀ màu sắc!")]
    assert audit(parts).excess == 2
    assert audit([parts[0], part("b", "Minh noi ve màu sắc.")]).excess == 0


def test_required_spans_only_exempt_their_actual_occurrences():
    required = "Chất vải dày và mềm."
    protected = [
        part(str(i), required + " Mỗi phần có một lời nối.", required=(required,)) for i in range(3)
    ]
    result = audit(protected)
    assert all(g.prefix != "chất vải dày" for g in result.groups)
    # The later identical sentence remains a style repetition: required facts
    # do not exempt the whole part or erase other occurrences of an opener.
    assert any(g.prefix == "mỗi phần có" for g in result.groups)


def test_part_and_sentence_counts_are_never_added_together():
    parts = [part("a", "Nói thêm một ý.", "intro"), part("b", "Nói thêm một câu.", "closing")]
    assert not audit(parts).groups  # two parts, not four occurrences


def test_within_part_repetition_is_a_warning_without_paid_repair():
    parts = [part("a", "Mọi người ơi, xem màu. Mọi người ơi, xem chất vải.", editable=True)]
    report = audit(parts)
    assert not report.groups and report.sentence_warnings == ("a",)
    assert not repair_candidates(parts, report)


def test_only_later_ai_parts_are_repair_candidates():
    parts = [part(str(i), f"Mọi người ơi, xem phần {i}.", editable=i != 1) for i in range(4)]
    candidates = repair_candidates(parts, audit(parts))
    assert [p.key for p in candidates] == ["2", "3"]
    assert parts[1].text == "Mọi người ơi, xem phần 1."


def test_replacement_must_reduce_excess_and_cannot_create_new_group():
    parts = [
        part("a", "Mọi người ơi, xem phần đầu."),
        part("b", "Mọi người ơi, xem màu nhé."),
        part("c", "Mọi người ơi, xem dáng nhé."),
        part("d", "Tiếp theo mình nói về size."),
    ]
    assert improves(parts, "c", "Còn một điểm nữa cần xem.")
    assert not improves(parts, "c", "Mọi người ơi, xem cách giặt.")
    assert not improves(parts, "c", "Tiếp theo mình nói về chất vải.")


@pytest.mark.parametrize(
    "count,expected", [(0, 0), (1, 1), (5, 1), (6, 2), (10, 2), (11, 3), (100, 3)]
)
def test_budget(count, expected):
    assert repair_budget(count) == expected


def test_duplicate_stable_part_keys_rejected():
    with pytest.raises(ValueError):
        audit([part("a", "Một câu đầu."), part("a", "Một câu khác.")])


@pytest.mark.parametrize(
    "new,allowed",
    [
        ("Mức giá là một trăm nghìn đồng.", True),
        ("Miễn phí toàn bộ sản phẩm.", False),
        ("Miễn phí, giá chỉ một trăm nghìn đồng.", False),
        ("Giá chỉ hai trăm nghìn đồng.", False),
        ("Giá chỉ một trăm nghìn đồng. Mua ngay.", False),
    ],
)
def test_style_only_repair_cannot_change_body_or_add_claims(new, allowed):
    assert opening_only("Giá chỉ một trăm nghìn đồng.", new) == allowed


def test_narrow_commitment_aliases_are_detected_without_editing_source():
    parts = [
        part("a", "Mình cam kết giữ nguyên điều kiện.", "intro"),
        part("b", "Shop cam kết giữ nguyên số liệu.", "assurance"),
        part("c", "Cam kết được nêu rõ trong chính sách.", "offer"),
    ]
    assert any(g.prefix == "cam kết" for g in audit(parts).groups)
    assert parts[0].text.startswith("Mình cam kết")
