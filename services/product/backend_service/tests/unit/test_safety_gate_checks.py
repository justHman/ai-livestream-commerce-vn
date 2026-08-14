"""Deterministic malformed / replay-flood / spam checks (tasks 3.1, 3.2).

Table-driven, NO network, NO real clock: every replay assertion passes an
explicit ``ts`` value. One assertion per test case.
"""

from __future__ import annotations

import pytest

from backend.application.safety_gate.checks import (
    MAX_TEXT_LENGTH,
    REPLAY_FLOOD_COUNT,
    REPLAY_WINDOW_SECONDS,
    ReplayWindow,
    check_malformed,
    check_replay_flood,
    check_spam,
)
from backend.application.safety_gate.decision import ReasonCode


# -- malformed ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "\n\t",
        "​​",  # zero-width spaces
        "\x00\x01",  # control chars only
    ],
)
def test_malformed_rejects_degenerate_text(text: str) -> None:
    assert check_malformed(text) == [ReasonCode.MALFORMED]


def test_malformed_rejects_none() -> None:
    assert check_malformed(None) == [ReasonCode.MALFORMED]


def test_malformed_rejects_oversized_text() -> None:
    assert check_malformed("a" * (MAX_TEXT_LENGTH + 1)) == [ReasonCode.MALFORMED]


def test_malformed_accepts_boundary_length() -> None:
    assert check_malformed("a" * MAX_TEXT_LENGTH) == []


def test_malformed_accepts_link_only_text() -> None:
    # A single URL is a legitimate comment; URL floods are a SPAM concern.
    assert check_malformed("https://example.com") == []


@pytest.mark.parametrize(
    "text",
    [
        "giá 299k đáng mua",
        "ok em ơi",
        "xem thử https://example.com nhé",
    ],
)
def test_malformed_accepts_normal_text(text: str) -> None:
    assert check_malformed(text) == []


# -- replay flood ------------------------------------------------------------


def _window() -> ReplayWindow:
    return ReplayWindow()


def test_replay_flood_accepts_first_comment() -> None:
    assert check_replay_flood("hello", _window(), ts=0.0) == []


def test_replay_flood_rejects_nth_identical_within_window() -> None:
    window = _window()
    for ts in range(REPLAY_FLOOD_COUNT - 1):
        assert check_replay_flood("trả ơn đi", window, ts=float(ts)) == []
    assert check_replay_flood("trả ơn đi", window, ts=float(REPLAY_FLOOD_COUNT)) == [
        ReasonCode.REPLAY_FLOOD
    ]


def test_replay_flood_different_text_never_counts() -> None:
    window = _window()
    for ts in range(REPLAY_FLOOD_COUNT + 1):
        assert check_replay_flood(f"comment {ts}", window, ts=float(ts)) == []


def test_replay_flood_normalizes_whitespace_and_case() -> None:
    window = _window()
    for ts in range(REPLAY_FLOOD_COUNT - 1):
        check_replay_flood("MUA NGAY ĐI", window, ts=float(ts))
    assert check_replay_flood("  mua   ngay đi ", window, ts=float(REPLAY_FLOOD_COUNT)) == [
        ReasonCode.REPLAY_FLOOD
    ]


def test_replay_flood_window_expiry_resets_count() -> None:
    window = ReplayWindow()
    for ts in range(REPLAY_FLOOD_COUNT - 1):
        check_replay_flood("spam", window, ts=float(ts))
    # Expired: everything older than the window is dropped, count resets.
    assert check_replay_flood("spam", window, ts=REPLAY_WINDOW_SECONDS + 100.0) == []


def test_replay_flood_none_window_disables_check() -> None:
    assert check_replay_flood("anything", None, ts=0.0) == []


# -- spam --------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Promotional template markers (curated multi-word markers).
        "like and subscribe để nhận quà",
        "tag a friend và share this video",
        "comment below to win phần thưởng",
        # All-caps + repeated punctuation (two combined signals).
        "MUA NGAY MUA NGAY MUA NGAY !!!!!!!!! GIÁ RẺ SỐC",
        # Emoji run + punctuation run (two combined signals).
        "siêu sale siêu sale siêu sale 🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉🎉!!!!!!!!!",
    ],
)
def test_spam_rejects_combined_signals(text: str) -> None:
    assert check_spam(text) == [ReasonCode.SPAM]


def test_spam_rejects_url_flood() -> None:
    assert check_spam("deal " + " ".join(f"https://deal{i}.com" for i in range(6))) == [
        ReasonCode.SPAM
    ]


@pytest.mark.parametrize(
    "text",
    [
        # Clean control: no single signal may reject on its own.
        "kem này dưỡng ẩm tốt lắm ạ",
        "MUA NGAY hôm nay giảm giá sâu",  # all-caps but short + no 2nd signal
        "https://example.com xem thử nhé",  # single URL is never spam
        "chúc mừng 🎉🎉🎉 chúc mừng 🎉🎉🎉",  # short emoji runs
        "Wow!!! quá đẹp luôn",  # short punctuation run
        "mua đi mua đi mua đi",  # repetition of a 3-char token is below threshold
    ],
)
def test_spam_accepts_weak_or_clean_signals(text: str) -> None:
    assert check_spam(text) == []


def test_spam_accepts_none() -> None:
    assert check_spam(None) == []


def test_spam_accepts_promotional_vietnamese_like_template() -> None:
    # "like" alone must never trigger; the marker requires the full template.
    assert check_spam("like bài này nha các bác") == []
