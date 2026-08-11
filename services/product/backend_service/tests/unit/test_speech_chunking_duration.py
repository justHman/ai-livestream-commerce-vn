"""SpeechDurationEstimator tests (task 3.5).

Proves compact written forms (prices, percentages, grouped numbers,
acronyms, English tokens) estimate differently from equal-length plain
Vietnamese words, that estimation never mutates the input text, and that
every estimate is a finite nonnegative number. Edge cases: empty, Unicode,
no whitespace, long tokens, unbalanced brackets, mixed text, and coefficient
validation.
"""

from __future__ import annotations

import math

import pytest

from backend.application.text_chunker.duration import (
    DurationCoefficients,
    SpeechDurationEstimator,
)


def test_empty_text_estimates_zero() -> None:
    assert SpeechDurationEstimator().estimate_ms("") == 0.0


def test_plain_vietnamese_estimate_is_finite_nonnegative() -> None:
    estimate = SpeechDurationEstimator().estimate_ms(
        "Xin chào mọi người, hôm nay shop giảm giá nhé!"
    )
    assert estimate >= 0.0
    assert math.isfinite(estimate)


def test_price_estimates_more_than_equal_length_plain_words() -> None:
    estimator = SpeechDurationEstimator()
    price_text = "giá 199.000đ nha"
    plain_text = "gía mua hàng ơi!"
    # Both strings are 16 chars; the price form ("199.000đ") must estimate
    # more than plain words without any compact form.
    assert len(price_text) == len(plain_text) == 16
    assert estimator.estimate_ms(price_text) > estimator.estimate_ms(plain_text)


def test_compact_price_differs_from_equal_length_plain_words() -> None:
    estimator = SpeechDurationEstimator()
    # Both strings are 23 chars; the price form must estimate differently.
    price_text = "Sản phẩm giá 199.000đ nha"
    plain_text = "Sản phẩm đang bán chạy ơi"
    assert len(price_text) == len(plain_text) == 25
    price = estimator.estimate_ms(price_text)
    plain = estimator.estimate_ms(plain_text)
    assert abs(price - plain) > 0


def test_percent_estimates_more_than_equal_length_plain_words() -> None:
    estimator = SpeechDurationEstimator()
    # Equal-length pair (21 chars each): "50%" reads as "năm mươi phần
    # trăm", longer than the plain "năm mươi" spelling, so the compact form
    # must estimate strictly more.
    percent_text = "Giảm giá đến 50% thôi"
    plain_text = "Giảm giá đến năm mươi"
    assert len(percent_text) == len(plain_text) == 21
    percent = estimator.estimate_ms(percent_text)
    plain = estimator.estimate_ms(plain_text)
    assert percent > plain


def test_grouped_number_estimates_more_than_plain_digits() -> None:
    estimator = SpeechDurationEstimator()
    grouped_text = "số 1.234.567 nha bạn"
    plain_text = "số 1234567 nha bạn ơ"
    assert len(grouped_text) == len(plain_text) == 20
    grouped = estimator.estimate_ms(grouped_text)
    plain = estimator.estimate_ms(plain_text)
    assert grouped > plain


def test_acronym_estimates_more_than_equal_length_plain_words() -> None:
    estimator = SpeechDurationEstimator()
    acronym_text = "dùng U.S.A. nha"
    plain_text = "dùng u.s.a. nha"
    assert len(acronym_text) == len(plain_text)
    acronym = estimator.estimate_ms(acronym_text)
    plain = estimator.estimate_ms(plain_text)
    assert acronym > plain


def test_english_token_estimates_more_than_equal_length_plain_words() -> None:
    estimator = SpeechDurationEstimator()
    english_text = "size free ship nha"
    plain_text = "sỉ dè phê síp nha "
    assert len(english_text) == len(plain_text) == 18
    english = estimator.estimate_ms(english_text)
    plain = estimator.estimate_ms(plain_text)
    assert english > plain


def test_estimation_never_mutates_input_text() -> None:
    text = "Giảm 50% cho 199.000đ và SKU-P004 nha!"
    before = text
    estimator = SpeechDurationEstimator()
    estimator.estimate_ms(text)
    assert text == before


def test_estimate_is_deterministic() -> None:
    estimator = SpeechDurationEstimator()
    text = "Hôm nay shop giảm 50% cho mọi đơn 199.000đ nhé!"
    assert estimator.estimate_ms(text) == estimator.estimate_ms(text)


def test_punctuation_pauses_increase_estimate() -> None:
    estimator = SpeechDurationEstimator()
    with_pauses = estimator.estimate_ms("Xin chào mọi người. Hôm nay giảm giá 50%! Còn 199.000đ.")
    without = estimator.estimate_ms("Xin chào mọi người Hôm nay giảm giá 50% Còn 199.000đ")
    assert with_pauses > without


def test_unicode_emoji_text_estimates_finite() -> None:
    estimate = SpeechDurationEstimator().estimate_ms("Xin chào bạn ơi 👋, chúc mừng năm mới! 🎉")
    assert math.isfinite(estimate)
    assert estimate >= 0.0


def test_long_no_whitespace_token_estimates_finite() -> None:
    estimate = SpeechDurationEstimator().estimate_ms("SKU-P004-" + "x" * 2000)
    assert math.isfinite(estimate)
    assert estimate > 0.0


def test_unbalanced_brackets_estimate_finite() -> None:
    estimate = SpeechDurationEstimator().estimate_ms("Còn hàng (size M thôi bạn, giá 50% đấy")
    assert math.isfinite(estimate)


def test_adversarial_digit_run_stays_finite() -> None:
    estimate = SpeechDurationEstimator().estimate_ms("số " + "9" * 10000 + " nha")
    assert math.isfinite(estimate)
    assert estimate > 0.0


def test_unbalanced_bracket_run_stays_finite() -> None:
    estimate = SpeechDurationEstimator().estimate_ms("(" * 500 + "abc")
    assert math.isfinite(estimate)
    assert estimate > 0.0


def test_percent_equal_length_forms_compare_by_length() -> None:
    estimator = SpeechDurationEstimator()
    percent_text = "Giảm giá đến 50% thôi"
    plain_text = "Giảm giá đến năm mươi"
    assert len(percent_text) == len(plain_text) == 21
    percent = estimator.estimate_ms(percent_text)
    plain = estimator.estimate_ms(plain_text)
    assert percent > plain


def test_acronym_equal_length_forms_compare_by_length() -> None:
    estimator = SpeechDurationEstimator()
    acronym = estimator.estimate_ms("dùng U.S.A. nha")
    plain = estimator.estimate_ms("dùng u.s.a. nha")
    assert len("dùng U.S.A. nha") == len("dùng u.s.a. nha")
    assert acronym > plain


def test_default_coefficients_are_finite_nonnegative() -> None:
    coefficients = DurationCoefficients()
    for name, value in coefficients.__dict__.items():
        assert value >= 0.0
        assert math.isfinite(value)


def test_invalid_coefficients_raise_validation_error() -> None:
    with pytest.raises(ValueError):
        DurationCoefficients(syllable_ms=-1.0)
    with pytest.raises(ValueError):
        DurationCoefficients(syllable_ms=float("inf"))
    with pytest.raises(ValueError):
        DurationCoefficients(syllable_ms=float("nan"))
    with pytest.raises(ValueError):
        DurationCoefficients(syllable_ms=True)
    with pytest.raises(ValueError):
        DurationCoefficients(syllable_ms="160")


def test_custom_coefficients_are_used_deterministically() -> None:
    slow = SpeechDurationEstimator(DurationCoefficients(syllable_ms=300.0))
    fast = SpeechDurationEstimator(DurationCoefficients(syllable_ms=100.0))
    text = "Xin chào mọi người"
    assert slow.estimate_ms(text) > fast.estimate_ms(text)


# ---------- review-wave regressions ----------


def test_plain_text_no_currency_false_positives() -> None:
    # Regression: the substring currency regex counted the "k" in "không"
    # (and "đ" in every Vietnamese word), inflating plain text. Plain text
    # must estimate identically at currency_multiplier=1 vs 9.
    plain = "không ai nói gì cả hôm nay đâu bạn ơi"
    est_flat = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=9.0))
    assert est_flat.estimate_ms(plain) == est_high.estimate_ms(plain)


def test_real_currency_forms_still_count() -> None:
    # The token-boundary currency regex must still catch standalone codes,
    # currency words, symbols, and digit-adjacent suffixes.
    text = "giá 199.000đ nha, mua bằng vnd, hay là đồng, 50k thôi"
    est_flat = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=9.0))
    assert est_high.estimate_ms(text) > est_flat.estimate_ms(text)


def test_huge_finite_coefficient_rejected() -> None:
    # Arbitrary finite coefficients must not be accepted: they could
    # overflow during exponentiation. Sensible upper limits are enforced.
    with pytest.raises(ValueError):
        DurationCoefficients(syllable_ms=60_001.0)
    with pytest.raises(ValueError):
        DurationCoefficients(number_multiplier=101.0)
    with pytest.raises(ValueError):
        DurationCoefficients(currency_multiplier=101.0)
    # Boundary values at the limit are accepted and stay finite.
    estimator = SpeechDurationEstimator(DurationCoefficients(syllable_ms=60_000.0))
    assert math.isfinite(estimator.estimate_ms("xin chào"))


def test_100k_char_input_is_fast_and_finite() -> None:
    # Guard against catastrophic regex/loop blowup on pathological input.
    import time

    text = "a" * 100_000
    estimator = SpeechDurationEstimator()
    start = time.perf_counter()
    estimate = estimator.estimate_ms(text)
    elapsed = time.perf_counter() - start
    assert math.isfinite(estimate)
    assert elapsed < 2.0


# ---------- adaptive-scoring regression (task 3.7 review) ----------


def test_ascii_multiplier_scales_ascii_english_words() -> None:
    # ASCII-only alphabetic words must receive ascii_multiplier: raising it
    # changes the estimate for a pure-ASCII English string.
    text = "hello world this is an english sentence"
    est_flat = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=9.0))
    assert est_high.estimate_ms(text) > est_flat.estimate_ms(text)


def test_ascii_multiplier_does_not_affect_accented_vietnamese() -> None:
    # Diacritic Vietnamese words are syllable units, never length-complexity
    # or ascii tokens: ascii_multiplier must not change their estimate.
    # Every word below carries a diacritic, so none is an ASCII token.
    text = "cảm ơn bạn ơi hôm này đi chợ nhé"
    est_flat = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=9.0))
    assert est_flat.estimate_ms(text) == est_high.estimate_ms(text)


def test_ascii_only_word_ship_scales_with_ascii_multiplier() -> None:
    # "ship" is ASCII-only, so it is English-like despite all its letters
    # appearing in the Vietnamese alphabet: ascii_multiplier must scale it.
    text = "ship"
    est_flat = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=9.0))
    assert est_high.estimate_ms(text) > est_flat.estimate_ms(text)


def test_accented_word_chao_is_invariant_to_ascii_multiplier() -> None:
    # "chào" carries a Vietnamese diacritic, so it is exactly one Vietnamese
    # syllable unit: ascii_multiplier must not change its estimate.
    text = "chào"
    est_flat = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(ascii_multiplier=9.0))
    assert est_flat.estimate_ms(text) == est_high.estimate_ms(text)


def test_symbol_currency_50_dollar_scales_with_currency_multiplier() -> None:
    # "$50" is a currency compact form (symbol prefix): raising
    # currency_multiplier must change its estimate.
    est_flat = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=9.0))
    assert est_high.estimate_ms("$50") > est_flat.estimate_ms("$50")


def test_code_currency_50_usd_scales_with_currency_multiplier() -> None:
    # "50 USD" is a currency compact form (standalone code): raising
    # currency_multiplier must change its estimate.
    est_flat = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(currency_multiplier=9.0))
    assert est_high.estimate_ms("50 USD") > est_flat.estimate_ms("50 USD")


def test_acronym_multiplier_scales_all_caps_token() -> None:
    # All-caps tokens ("TTS") are acronyms: raising acronym_multiplier must
    # change their estimate.
    text = "dùng TTS nha"
    est_flat = SpeechDurationEstimator(DurationCoefficients(acronym_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(acronym_multiplier=9.0))
    assert est_high.estimate_ms(text) > est_flat.estimate_ms(text)


def test_acronym_multiplier_does_not_scale_lowercase_equivalent() -> None:
    # The lowercase equivalent ("tts") is not an acronym: acronym_multiplier
    # must not change its estimate.
    text = "dùng tts nha"
    est_flat = SpeechDurationEstimator(DurationCoefficients(acronym_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(acronym_multiplier=9.0))
    assert est_flat.estimate_ms(text) == est_high.estimate_ms(text)


def test_number_multiplier_scales_ungrouped_number() -> None:
    # A bare ungrouped number ("50") must activate number_multiplier.
    text = "Giảm giá 50 phần trăm"
    est_flat = SpeechDurationEstimator(DurationCoefficients(number_multiplier=1.0))
    est_high = SpeechDurationEstimator(DurationCoefficients(number_multiplier=9.0))
    assert est_high.estimate_ms(text) > est_flat.estimate_ms(text)


def test_max_coefficients_on_100k_compact_input_stay_finite() -> None:
    # Maximum accepted coefficients on a compact pathological input must
    # produce a finite nonnegative estimate (the bounded-count guarantee).
    text = "SKU-P004-" + "50" * 50_000 + "-abc"
    estimator = SpeechDurationEstimator(
        DurationCoefficients(
            syllable_ms=60_000.0,
            number_multiplier=100.0,
            currency_multiplier=100.0,
            percent_multiplier=100.0,
            acronym_multiplier=100.0,
            ascii_multiplier=100.0,
            punctuation_pause_ms=60_000.0,
            comma_pause_ms=60_000.0,
            phrase_break_pause_ms=60_000.0,
        )
    )
    estimate = estimator.estimate_ms(text)
    assert estimate >= 0.0
    assert math.isfinite(estimate)
