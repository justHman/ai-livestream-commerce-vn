"""Pure policy scoring tests (task 3.6) for adaptive Vietnamese chunking.

Covers the required pure policy cases: linguistic quality vs duration,
protected-span exclusion, ``target_chars`` tie-break/fallback, hard-cap
enforcement, and non-finite-estimate rejection. All selection is
deterministic and never mutates the input.
"""

from __future__ import annotations

import math

import pytest

from backend.application.text_chunker.boundaries import (
    BoundaryCandidate,
    CandidateKind,
    extract_candidates,
)
from backend.application.text_chunker.duration import SpeechDurationEstimator
from backend.application.text_chunker.policy import (
    TARGET_DURATION_MS,
    AdaptiveAnalysisError,
    chunk_decision_reason,
    score_boundary,
    select_boundary,
)


def _candidates(text: str, max_chars: int = 80) -> list[BoundaryCandidate]:
    return extract_candidates(text, max_chars)


def _selected_end(
    text: str,
    *,
    min_chars: int = 12,
    target_chars: int = 40,
    max_chars: int = 80,
) -> int | None:
    selected = select_boundary(
        text,
        _candidates(text, max_chars),
        estimator=SpeechDurationEstimator(),
        target_chars=target_chars,
        max_chars=max_chars,
        min_chars=min_chars,
    )
    return None if selected is None else selected.candidate.end


def test_empty_text_selects_none() -> None:
    assert (
        select_boundary(
            "",
            [],
            estimator=SpeechDurationEstimator(),
            target_chars=40,
            max_chars=80,
            min_chars=12,
        )
        is None
    )


# ---------- linguistic quality vs duration ----------


def test_stronger_kind_outranks_weaker_regardless_of_duration() -> None:
    # A sentence end and a later whitespace: the sentence (kind 2) must win
    # even if its head is far from the duration target and the whitespace is
    # closer.
    text = "Xin chào mọi người. Tôi cần mua áo khoác xinh"
    sentence_end = text.index(".") + 1
    whitespace_end = text.index(" khoác") + 1
    assert sentence_end < whitespace_end
    estimator = SpeechDurationEstimator()
    sentence_score = score_boundary(
        text, BoundaryCandidate(CandidateKind.SENTENCE, sentence_end, False), estimator, 40
    )
    white_score = score_boundary(
        text, BoundaryCandidate(CandidateKind.WHITESPACE, whitespace_end, False), estimator, 40
    )
    assert sentence_score < white_score


def test_duration_proximity_breaks_kind_ties() -> None:
    # Two whitespace candidates of the same kind: the head whose estimated
    # duration is closer to TARGET_DURATION_MS wins. Constructed so the
    # earlier whitespace lands a head duration far from the target and the
    # later one lands near it — the nearer-duration candidate must win even
    # though it is further from target_chars.
    estimator = SpeechDurationEstimator()
    text = "xin chào mọi người hôm nay tôi đi chợ sớm mua đồ"
    whitespaces = [c for c in _candidates(text) if c.kind == CandidateKind.WHITESPACE]
    assert len(whitespaces) >= 2
    a, b = whitespaces[0], whitespaces[-1]
    da = estimator.estimate_ms(text[: a.end])
    db = estimator.estimate_ms(text[: b.end])
    # Deterministic probe: a and b must differ, and b's head must be closer
    # to the soft duration target than a's (a is a very short head).
    assert db > da
    assert abs(db - TARGET_DURATION_MS) < abs(da - TARGET_DURATION_MS)
    score_a = score_boundary(text, a, estimator, 40)
    score_b = score_boundary(text, b, estimator, 40)
    assert score_b < score_a


def test_compact_price_sizes_chunk_by_spoken_length_not_characters() -> None:
    # "199.000đ" (a price) must inflate estimated duration of heads that
    # contain it, so a whitespace boundary right after the price scores
    # worse than the same boundary in plain text — the scorer accounts for
    # spoken complexity, not raw characters.
    estimator = SpeechDurationEstimator()
    price_text = "Giá 199.000đ hôm nay"
    plain_text = "Giá hai mốt hôm nay"
    price_ws = [c for c in _candidates(price_text) if c.kind == CandidateKind.WHITESPACE]
    plain_ws = [c for c in _candidates(plain_text) if c.kind == CandidateKind.WHITESPACE]
    # The whitespace right after "199.000đ" is at a different offset than the
    # plain-text counterpart; assert the price head estimates much longer.
    assert estimator.estimate_ms(price_text[: price_ws[2].end]) > estimator.estimate_ms(
        plain_text[: plain_ws[2].end]
    )


# ---------- protected-span exclusion ----------


def test_protected_candidate_not_selected_when_safe_exists() -> None:
    # The "!" inside quotes is protected; a later safe boundary exists, so the
    # protected one must never be auto-committed.
    text = 'Cô ấy nói "Đi ngay!" rồi cười. Sau đó im lặng.'
    ends = _candidates(text)
    protected_ends = [c.end for c in ends if c.protected and c.kind == CandidateKind.SENTENCE]
    assert protected_ends  # guard: the protected sentence candidate exists
    selected = select_boundary(
        text,
        ends,
        estimator=SpeechDurationEstimator(),
        target_chars=40,
        max_chars=80,
        min_chars=12,
    )
    assert selected is not None
    assert not selected.candidate.protected


def test_protected_strong_never_auto_committed_before_safe_boundary() -> None:
    # The only sentence boundary is protected (URL dot); with no safe strong
    # boundary and no cap pressure, adaptive selection keeps buffering.
    text = "Xem tại https://example.com xong"
    selected = select_boundary(
        text,
        _candidates(text),
        estimator=SpeechDurationEstimator(),
        target_chars=40,
        max_chars=80,
        min_chars=12,
    )
    assert selected is None


# ---------- target_chars tie-break/fallback ----------


def test_target_chars_breaks_duration_ties() -> None:
    # A constant-duration estimator forces an exact duration tie across all
    # candidates, so the character-distance tie-break decides: the candidate
    # closest to target_chars wins.
    class _ConstantEstimator:
        def estimate_ms(self, text: str) -> float:  # noqa: ARG002
            return TARGET_DURATION_MS

    estimator = _ConstantEstimator()
    text = "xin chào bạn ơi hôm nay"
    whitespaces = [c for c in _candidates(text) if c.kind == CandidateKind.WHITESPACE]
    assert len(whitespaces) >= 2
    a = whitespaces[2]  # end 13 — exactly on target
    b = whitespaces[0]  # end 4 — far from target
    assert a.end == 13
    score_a = score_boundary(text, a, estimator, target_chars=13)
    score_b = score_boundary(text, b, estimator, target_chars=13)
    assert score_a < score_b


def test_target_chars_single_tie_break_deterministic() -> None:
    # Same inputs -> same selection every time.
    text = "xin chào mọi người hôm nay tôi đi chợ"
    ends1 = _selected_end(text)
    ends2 = _selected_end(text)
    assert ends1 == ends2


# ---------- hard cap ----------


def test_hard_cap_wins_when_no_natural_boundary_before_cap() -> None:
    # 100 'a's, cap 80: the only candidate is the hard cap; adaptive must
    # select it so the buffer drains into <= 80-char chunks.
    text = "a" * 100
    selected = select_boundary(
        text,
        _candidates(text, 80),
        estimator=SpeechDurationEstimator(),
        target_chars=40,
        max_chars=80,
        min_chars=12,
    )
    assert selected is not None
    assert selected.candidate.end == 80
    assert selected.forced is True


def test_hard_cap_forced_split_prefers_best_natural_boundary() -> None:
    # Over-cap buffer with weak natural boundaries: the best one by composite
    # score is committed so the head stays <= max_chars on a word boundary.
    text = "xin chào " * 15  # len 135 > 80, whitespace at 9,18,...
    selected = select_boundary(
        text,
        _candidates(text, 80),
        estimator=SpeechDurationEstimator(),
        target_chars=40,
        max_chars=80,
        min_chars=12,
    )
    assert selected is not None
    assert selected.forced is True
    assert selected.candidate.end <= 80


def test_hard_cap_with_protected_only_forces_protected_split() -> None:
    # The whole buffer is one protected token over the cap: no safe natural
    # boundary exists, so the strongest protected boundary (the cap) is
    # accepted as the required forced split.
    text = "SKU-P004-" + "x" * 90  # one long protected token, len 99 > 80
    selected = select_boundary(
        text,
        _candidates(text, 80),
        estimator=SpeechDurationEstimator(),
        target_chars=40,
        max_chars=80,
        min_chars=12,
    )
    assert selected is not None
    assert selected.candidate.end == 80
    assert selected.forced is True


def test_no_boundary_selected_below_min() -> None:
    # A short buffer with only weak boundaries and no cap pressure keeps
    # buffering.
    text = "xin chào bạn"
    selected = select_boundary(
        text,
        _candidates(text),
        estimator=SpeechDurationEstimator(),
        target_chars=40,
        max_chars=80,
        min_chars=12,
    )
    assert selected is None


# ---------- estimator failure ----------


class _BrokenEstimator:
    def estimate_ms(self, text: str) -> float:  # noqa: ARG002
        return float("nan")


def test_non_finite_estimate_raises_adaptive_analysis_error() -> None:
    text = "Xin chào mọi người. Tôi cần mua."
    with pytest.raises(AdaptiveAnalysisError):
        score_boundary(
            text, BoundaryCandidate(CandidateKind.SENTENCE, 20, False), _BrokenEstimator(), 40
        )


def test_score_is_finite_and_deterministic() -> None:
    text = "Xin chào mọi người. Hôm nay shop giảm 50%!"
    estimator = SpeechDurationEstimator()
    for candidate in _candidates(text):
        score = score_boundary(text, candidate, estimator, 40)
        assert math.isfinite(score)
    assert _selected_end(text) == _selected_end(text)


def test_decision_reason_mapping() -> None:
    assert chunk_decision_reason(CandidateKind.PARAGRAPH).value == "paragraph"
    assert chunk_decision_reason(CandidateKind.SENTENCE).value == "sentence"
    assert chunk_decision_reason(CandidateKind.COMMA).value == "clause"
    assert chunk_decision_reason(CandidateKind.WHITESPACE).value == "target"
