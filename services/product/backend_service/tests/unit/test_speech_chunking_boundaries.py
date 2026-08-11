"""Table-driven tests for text_chunker.boundaries (tasks 3.1-3.3).

Pure candidate extraction: every candidate ``end`` is an exact slice end
offset of the ORIGINAL string (``text[:end]``/``text[end:]`` join back to
the exact input). Assertions cover multi-sentence paragraphs, clauses,
prices/percentages/decimals, product names, SKUs, acronyms, mixed VI/EN,
quotes, parentheses, plus exhaustive edge cases: empty text, Unicode, no
whitespace, long tokens, unbalanced delimiters, overlapping spans, off-by-
one, sorted/deduped output, exact reconstruction, and no-false-decimal
sentence splits.
"""

from __future__ import annotations

from backend.application.text_chunker.boundaries import (
    BoundaryCandidate,
    CandidateKind,
    extract_candidates,
    protected_spans,
)


# ---------- helpers ----------


def by_kind(text: str, kind: CandidateKind, max_chars: int = 80) -> list[BoundaryCandidate]:
    return [c for c in extract_candidates(text, max_chars) if c.kind == kind]


def ends_of(kind: CandidateKind, text: str, max_chars: int = 80) -> list[int]:
    return [c.end for c in by_kind(text, kind, max_chars)]


def kinds(text: str, max_chars: int = 80) -> list[str]:
    return [c.kind.name for c in extract_candidates(text, max_chars)]


# ---------- 3.1 candidate classes ----------


def test_multi_sentence_paragraph_candidates():
    text = "Xin chào mọi người. Hôm nay có ưu đãi lớn! Bạn cần gì?"
    # Three real sentence ends; the final "?" lands exactly at len(text) so
    # no hard cap is needed. Offsets are derived from the text itself so
    # diacritic lengths can't drift.
    expected = [text.index("người.") + 6, text.index("lớn!") + 4, len(text)]
    assert ends_of(CandidateKind.SENTENCE, text) == expected
    assert CandidateKind.HARD_CAP not in [c.kind for c in extract_candidates(text, 80)]


def test_strength_ranks_kinds_deterministically():
    text = "Đoạn một. Câu hai, (ba)!"
    candidates = extract_candidates(text, 80)
    assert all(c.strength == int(c.kind) for c in candidates)
    by_end = {c.end: c for c in candidates}
    # SENTENCE (2) outranks COMMA (4); WHITESPACE (6) is weakest here.
    assert by_end[text.index("một.") + 4].strength == 2
    assert by_end[text.index("hai,") + 4].strength == 4
    assert max(c.strength for c in candidates) == 6
    # A forced split beyond the cap is the weakest kind (7).
    cap = by_kind("a" * 12, CandidateKind.HARD_CAP, max_chars=10)
    assert cap[0].strength == 7


def test_clause_semicolon_and_colon_candidates():
    text = "Mua một; mua hai: đều tốt"
    assert ends_of(CandidateKind.CLAUSE, text) == [text.index(";") + 1, text.index(":") + 1]


def test_comma_candidates():
    text = "Tôi cần áo khoác, quần jean, và giày"
    expected = [text.index(",") + 1, text.index(", và") + 1]
    assert ends_of(CandidateKind.COMMA, text) == expected


def test_vietnamese_cue_is_feature_not_decision():
    # The cue word upgrades the whitespace candidate's kind; the candidate
    # itself remains at the SAME whitespace offset.
    cued = "không những thế và thôi"
    cue_offset = cued.index(" và ") + 1
    assert ends_of(CandidateKind.VIETNAMESE_CUE, cued) == [cue_offset]
    # Only the cue-upgraded offset leaves the WHITESPACE set; the other
    # whitespace positions remain plain whitespace candidates.
    all_ws = [i + 1 for i, ch in enumerate(cued) if ch.isspace()]
    assert ends_of(CandidateKind.WHITESPACE, cued) == [e for e in all_ws if e != cue_offset]
    # No cue-only split is invented: every candidate end is a real
    # whitespace position (or the text end).
    plain = "không những thế thôi"
    assert ends_of(CandidateKind.WHITESPACE, plain) == [
        i + 1 for i, ch in enumerate(plain) if ch.isspace()
    ]


def test_whitespace_candidates_without_cue():
    assert ends_of(CandidateKind.WHITESPACE, "hai từ thôi") == [4, 7]


def test_hard_cap_candidate_when_text_exceeds_cap():
    # No whitespace at all: the forced split lands exactly at the cap and
    # survives dedupe (nothing weaker shares its end).
    text = "a" * 12
    assert kinds(text, max_chars=10)[-1] == "HARD_CAP"
    assert ends_of(CandidateKind.HARD_CAP, text, max_chars=10) == [10]


def test_no_hard_cap_when_text_fits():
    text = "vừa đủ ngắn"
    assert CandidateKind.HARD_CAP not in [c.kind for c in extract_candidates(text, 80)]


def test_hard_cap_prefers_last_whitespace_at_or_before_cap():
    # text is 34 chars, cap 15: no HARD_CAP candidate, because whitespace
    # exists at 13 (<= 15); the forced split would land there. When the cap
    # lands ON a whitespace, the natural candidate wins.
    text = "xin chào tất cả các bạn ơi hôm nay"
    assert len(text) == 34
    assert 13 in ends_of(CandidateKind.WHITESPACE, text, max_chars=15)
    assert ends_of(CandidateKind.HARD_CAP, text, max_chars=15) == []
    assert text[12] == " "


def test_hard_cap_inside_protected_span_flags_protected():
    # max_chars=6 forces a split inside "SKU-P004"; the forced candidate is
    # flagged protected because it cuts strictly inside the protected span.
    text = "SKU-P004"
    caps = by_kind(text, CandidateKind.HARD_CAP, max_chars=6)
    assert len(caps) == 1
    assert caps[0].end == 6
    assert caps[0].protected is True
    # A forced split between plain words stays unprotected.
    safe = by_kind("aaaaaaaa bbbb", CandidateKind.HARD_CAP, max_chars=8)
    assert len(safe) == 1
    assert safe[0].end == 8
    assert safe[0].protected is False


def test_hard_cap_no_whitespace_cuts_exact_cap():
    text = "a" * 25
    assert ends_of(CandidateKind.HARD_CAP, text, max_chars=10) == [10]


# ---------- 3.2 protected spans ----------


def test_decimal_punctuation_not_a_sentence_candidate():
    text = "Giá là 199.000đ hôm nay."
    # The "199.000đ" dot is protected; the real sentence end after "hôm
    # nay." is unprotected.
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert len(sentences) == 2
    assert sentences[0].protected is True
    assert sentences[0].end == text.index(".000") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)


def test_float_decimal_not_a_sentence_candidate():
    text = "Pi bằng 3.14 và xấp xỉ 22/7."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # The "3.14" dot is protected; the final "." after "22/7" is real.
    assert len(sentences) == 2
    assert sentences[0].protected is True
    assert sentences[0].end == text.index(".14") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)


def test_currency_and_percent_tokens_kept():
    text = "Giá 199.000đ, giảm 50%, chỉ còn 99.000đ."
    # Decimal dots protected; the two commas are real, unprotected clause
    # ends; the final "." is the one real sentence end. (The "50%" percent
    # token leaves a protected "." in its span.)
    assert ends_of(CandidateKind.COMMA, text) == [13, 23]
    assert all(not c.protected for c in by_kind(text, CandidateKind.COMMA))
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # "199.000đ" dot and "99.000đ." dot are protected; the final "." after
    # the second price is the only unprotected sentence end.
    assert len(sentences) == 3
    assert sentences[0].protected is True
    assert sentences[1].protected is True
    assert sentences[2].protected is False
    assert sentences[2].end == len(text)


def test_url_and_email_punctuation_protected():
    text = "Xem tại https://example.com/a.b và gửi mail@example.com."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # The "com." / "a.b" / email dots are protected; only the real final
    # dot is an unprotected candidate.
    assert len(sentences) == 4
    assert all(s.protected for s in sentences[:-1])
    assert sentences[-1].protected is False
    assert sentences[-1].end == len(text)


def test_url_final_period_is_safe_boundary():
    # The URL span is trimmed of terminal punctuation: the "." that ends
    # the sentence right after "https://example.com" is a real, unprotected
    # end; the dot inside "example.com" stays protected.
    text = "Xem tại https://example.com. Sau đó xong!"
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert [c.end for c in sentences] == [
        text.index("example.") + 8,
        text.index("com.") + 4,
        len(text),
    ]
    assert sentences[0].protected is True
    assert sentences[1].protected is False
    assert sentences[2].protected is False


def test_url_internal_dots_remain_protected():
    text = "Link https://example.com/a.b và mail@example.com.vn xong."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # URL and email domain dots are protected; the final "." after
    # "xong" is the only unprotected sentence end.
    assert len(sentences) == 5
    assert all(s.protected for s in sentences[:-1])
    assert sentences[-1].protected is False
    assert sentences[-1].end == len(text)


def test_nested_parentheses_balanced_only():
    text = "Xem (sản phẩm (mới, giá tốt!)) đã về."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # The "!" inside the nested parens is protected; the final "." is real.
    assert len(sentences) == 2
    assert sentences[0].protected is True
    assert sentences[0].end == text.index("!") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)
    # Unbalanced parens protect nothing.
    unbalanced = "Xem (sản phẩm (mới, giá tốt đã về."
    unbal = by_kind(unbalanced, CandidateKind.SENTENCE)
    assert len(unbal) == 1
    assert unbal[0].protected is False
    assert unbal[0].end == len(unbalanced)


def test_multiple_quote_pairs_protected():
    text = 'Nói "Đi ngay!" rồi ' + chr(39) + 'cười "lớn"!' + chr(39) + " sau đó im."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # Both quoted "!" are protected; the final "." is the real end.
    assert len(sentences) == 3
    assert all(s.protected for s in sentences[:-1])
    assert sentences[-1].protected is False
    assert sentences[-1].end == len(text)


def test_crlf_and_cr_only_paragraph_candidates():
    text = "Dòng một\r\nDòng hai\rDòng ba\nDòng bốn"
    paragraphs = by_kind(text, CandidateKind.PARAGRAPH)
    # One candidate per line break, exact end offsets, no duplicates.
    assert [c.end for c in paragraphs] == [
        text.index("một\r\n") + len("một\r\n"),
        text.index("hai\r") + len("hai\r"),
        text.index("ba\n") + len("ba\n"),
    ]
    assert text[: text.index("hai\r") + len("hai\r")] == "Dòng một\r\nDòng hai\r"


def test_sku_token_punctuation_protected():
    text = "Mã SKU-P004, size M, còn hàng."
    # The SKU span covers "SKU-P004"; the commas after it are real.
    assert ends_of(CandidateKind.COMMA, text) == [12, 20]
    assert all(not c.protected for c in by_kind(text, CandidateKind.COMMA))
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert len(sentences) == 1
    assert sentences[0].end == len(text)


def test_acronym_punctuation_protected():
    text = "AI và TTS là công nghệ mới. OK."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # "OK." acronym is a protected span; splitting at its end (after the
    # dot) is safe (whole token in head); the "mới." dot is a real end.
    assert len(sentences) == 2
    assert sentences[0].protected is False
    assert sentences[0].end == text.index("mới.") + 4
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)


def test_common_abbreviation_dot_protected():
    text = "Gặp Dr. Nam lúc 2h. OK."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # "Dr." is a protected span; its dot stays inside it, so splitting AT
    # the span end keeps the whole token in the head — that boundary is
    # safe. The "2h." and "OK." dots are real sentence ends.
    assert len(sentences) == 3
    assert sentences[0].end == text.index("Dr.") + 3
    assert sentences[1].end == text.index("2h.") + 3
    assert sentences[2].end == len(text)
    assert all(not s.protected for s in sentences)


def test_quoted_region_punctuation_protected():
    text = 'Cô ấy nói "Đi ngay!" rồi cười.'
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # The "!" inside quotes is protected (flagged, scorer excludes); the
    # final "." is the real unprotected sentence end.
    assert len(sentences) == 2
    assert sentences[0].protected is True
    assert sentences[0].end == text.index("!") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)


def test_parenthesized_region_punctuation_protected():
    text = "Sản phẩm (hàng mới, giá tốt!) đã về."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # The "!" inside parens is protected; the final "." is the real end.
    assert len(sentences) == 2
    assert sentences[0].protected is True
    assert sentences[0].end == text.index("!") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)


def test_closing_quote_does_not_suppress_safe_boundary_after_it():
    text = 'Cô ấy nói "Đi ngay!" rồi cười. Sau đó im lặng.'
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # The "!" inside quotes is protected; both following "." ends are real,
    # unprotected (closing quotes never suppress a safe boundary after them).
    assert len(sentences) == 3
    assert sentences[0].protected is True
    assert sentences[0].end == text.index("!") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == text.index("cười.") + 5
    assert sentences[2].protected is False
    assert sentences[2].end == len(text)


def test_sku_and_number_spans_cover_expected_ranges():
    text = "Giá 199.000đ hôm nay, SKU-P004, 50%."
    spans = protected_spans(text)
    # Digit runs with separators are fully covered.
    for token in ["199.000đ", "SKU-P004", "50"]:
        start = text.index(token)
        assert any(
            span_start <= start and start + len(token) <= span_end for span_start, span_end in spans
        )


# ---------- mixed VI/EN and product names ----------


def test_mixed_vietnamese_english_text():
    text = "Giá của iPhone 15 Pro Max là 25.990.000đ. Rất hợp lý!"
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # Grouped-number dots ("25.990.000đ") are protected; the final two
    # sentence ends are real.
    assert len(sentences) == 4
    assert all(s.protected for s in sentences[:2])
    assert sentences[2].protected is False
    assert sentences[2].end == text.index("đ.") + 2
    assert sentences[3].protected is False
    assert sentences[3].end == len(text)


def test_product_name_with_space_not_falsely_split():
    text = "Áo khoác dù 2 lớp đã về hàng."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert len(sentences) == 1
    assert sentences[0].end == len(text)


# ---------- exhaustive edge cases ----------


def test_empty_text_returns_no_candidates():
    assert extract_candidates("", 80) == []
    assert protected_spans("") == []


def test_unicode_text_preserved_exactly():
    text = "Xin chào 👋. Giá 1.000đ!"
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # Real ends: after "👋." (offset 11), the "1.000đ" dot (protected, end
    # = index of the dot + 1), and the final "!".
    assert [c.end for c in sentences] == [11, text.index(".000") + 1, len(text)]
    assert sentences[1].protected is True
    # Exact reconstruction through every candidate.
    assert all(text[: c.end] + text[c.end :] == text for c in extract_candidates(text, 80))


def test_no_whitespace_long_token_only_hard_cap():
    text = "a" * 100
    candidates = extract_candidates(text, 80)
    assert [c.kind.name for c in candidates] == ["HARD_CAP"]
    assert candidates[0].end == 80


def test_unbalanced_open_quote_is_not_protected():
    text = 'Cô ấy nói "Đi ngay rồi cười.'
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # No balanced quote pair exists, so the final "." stays a real sentence
    # end (it is the only "." in the text).
    assert len(sentences) == 1
    assert sentences[0].end == len(text)


def test_unbalanced_open_parenthesis_is_not_protected():
    text = "Sản phẩm (hàng mới đã về."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert len(sentences) == 1
    assert sentences[0].end == len(text)


def test_overlapping_spans_merged_sorted():
    spans = protected_spans("gửi cho a.b@example.com nhé")
    flat = [offset for start, end in spans for offset in range(start, end)]
    assert flat == sorted(flat)
    assert len(flat) == len(set(flat))


def test_off_by_one_end_offsets_are_exclusive():
    text = "Một. Hai. Ba."
    for candidate in extract_candidates(text, 80):
        if candidate.kind == CandidateKind.SENTENCE:
            assert text[candidate.end - 1] in ".!?"
        elif candidate.kind == CandidateKind.HARD_CAP:
            continue
        else:
            assert text[candidate.end - 1].isspace()


def test_candidates_sorted_ascending_and_deduped():
    text = "Một. Hai, ba. Bốn"
    candidates = extract_candidates(text, 80)
    offsets = [c.end for c in candidates]
    assert offsets == sorted(offsets)
    assert len(offsets) == len(set(offsets))
    # A whitespace candidate coinciding with a sentence end keeps the
    # stronger kind only (one candidate per end).
    assert len([c for c in candidates if c.end == 5]) == 1


def test_exact_reconstruction_of_every_candidate():
    text = "Xin chào mọi người. Hôm nay shop có SKU-P004 giá 199.000đ, giảm 50%! OK?"
    for candidate in extract_candidates(text, 80):
        assert text[: candidate.end] + text[candidate.end :] == text


def test_no_false_decimal_sentence_split():
    for text in [
        "199.000đ",
        "3.14",
        "1.000.000đ",
        "50%",
        "SKU-P004",
        "a.b@example.com",
        "https://example.com/a.b",
    ]:
        for candidate in extract_candidates(text, 80):
            if candidate.kind == CandidateKind.SENTENCE:
                assert candidate.protected is True


def test_hard_cap_split_within_protected_span_remains_representable():
    # max_chars=6 forces a split inside the protected token "SKU-P004"
    # (8 chars, no whitespace at or before the cap); the forced split is
    # still a real slice offset, keeps HARD_CAP kind, and reconstructs
    # exactly.
    text = "SKU-P004"
    caps = by_kind(text, CandidateKind.HARD_CAP, max_chars=6)
    assert len(caps) == 1
    assert caps[0].end == 6
    assert text[: caps[0].end] + text[caps[0].end :] == text


# ---------- review-wave regressions ----------


def test_sku_like_tokens_with_lowercase_are_protected():
    # Lowercase/mixed SKU forms were missed because the first scan only
    # added uppercase acronym tokens and special-range extension only
    # extended existing spans; the explicit SKU check now protects them.
    text = "mã sku-p004 và abc_1 còn abc/9#x"
    spans = protected_spans(text)
    for token in ["sku-p004", "abc_1", "abc/9#x"]:
        start = text.index(token)
        assert any(
            span_start <= start and start + len(token) <= span_end for span_start, span_end in spans
        )


def test_natural_slash_and_plain_words_are_not_sku():
    # No letter+digit combination around the separator: natural slash forms
    # and plain words stay unprotected.
    for text in ["hôm/nay", "a/b", "mua xong", "không sao"]:
        assert protected_spans(text) == []


def test_sku_hard_cap_flag_protected():
    text = "sku-p004"
    caps = by_kind(text, CandidateKind.HARD_CAP, max_chars=6)
    assert len(caps) == 1
    assert caps[0].end == 6
    assert caps[0].protected is True
    assert caps[0].hard_cap is True


def test_percent_sign_kept_in_protected_span():
    # The "50%" span must cover the percent sign itself, not just "50".
    text = "Giảm 50% thôi"
    spans = protected_spans(text)
    start = text.index("50%")
    assert any(
        span_start <= start and start + len("50%") <= span_end for span_start, span_end in spans
    )


def test_parentheses_containing_quotes_close_outer_pair():
    # Regression: the old shared stack let a top quote prevent ")" from
    # closing the paren, so no span was produced. Now each stack entry
    # carries its expected closer, so the quote closes first and the paren
    # closes after.
    text = '(Nói "đi ngay!") rồi im.'
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert len(sentences) == 2
    assert sentences[0].protected is True
    assert sentences[0].end == text.index("!") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)


def test_curly_quote_pairs_protected():
    text = "Cô ấy nói “Đi ngay!” rồi cười."
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert len(sentences) == 2
    assert sentences[0].protected is True
    assert sentences[0].end == text.index("!") + 1
    assert sentences[1].protected is False
    assert sentences[1].end == len(text)


def test_mismatched_closer_does_not_block_outer_close():
    # ")" does not match the quote on top; it must be ignored, not consumed,
    # so the closing quote still closes its pair.
    text = 'Nói "đi ngay" rồi) cười.'
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert sentences[-1].protected is False
    assert sentences[-1].end == len(text)


def test_url_followed_by_closing_paren_trims_external_delimiter():
    text = "Xem (https://example.com). Sau đó xong!"
    sentences = by_kind(text, CandidateKind.SENTENCE)
    # The URL internal dot is protected; the ")" and "." right after the URL
    # are external and yield a real, unprotected sentence end; the final "!"
    # is the last end.
    assert sentences[0].protected is True
    assert text[sentences[0].end - 1] == "."
    assert sentences[1].protected is False
    assert text[sentences[1].end - 1] == "."
    assert sentences[2].protected is False
    assert sentences[2].end == len(text)


def test_url_with_internal_paren_keeps_own_closer():
    # A URL containing "(" (wikipedia-style) must keep its own ")" protected.
    text = "Xem https://en.wikipedia.org/wiki/Foo_(bar). Xong!"
    spans = protected_spans(text)
    closer = text.index("(bar)") + len("(bar)") - 1  # position of ")"
    assert any(start <= closer < end for start, end in spans)
    # Only the final "!" is an unprotected sentence end.
    sentences = by_kind(text, CandidateKind.SENTENCE)
    assert sentences[-1].protected is False
    assert sentences[-1].end == len(text)


def test_whitespace_inside_protected_span_flagged():
    text = 'Nói "đi ngay" rồi'
    inside = by_kind(text, CandidateKind.WHITESPACE)
    assert len(inside) == 3
    # The whitespace between "đi" and "ngay" sits inside the quoted span and
    # is flagged; the leading and trailing whitespace are outside.
    assert inside[0].end == text.index(" ") + 1
    assert inside[0].protected is False
    assert inside[1].protected is True
    assert inside[2].protected is False


def test_cue_inside_protected_span_flagged():
    text = "(không những thế và thôi)"
    cued = by_kind(text, CandidateKind.VIETNAMESE_CUE)
    assert len(cued) == 1
    assert cued[0].protected is True


def test_paragraph_inside_protected_span_flagged():
    text = '(Nói "dòng một\ndòng hai") rồi im.'
    paragraphs = by_kind(text, CandidateKind.PARAGRAPH)
    assert len(paragraphs) == 1
    assert paragraphs[0].protected is True


def test_hard_cap_flag_preserved_when_stronger_kind_shares_end():
    # "aaaa.bbbb" (9 chars, no whitespace) with cap 5: the forced split
    # lands exactly on the "." end (5), which is also a SENTENCE candidate;
    # the merge keeps the stronger kind (SENTENCE) but preserves the
    # hard-cap flag so forced status stays representable.
    text = "aaaa.bbbb"
    candidates = extract_candidates(text, max_chars=5)
    at_end = [c for c in candidates if c.end == 5]
    assert len(at_end) == 1
    assert at_end[0].kind == CandidateKind.SENTENCE
    assert at_end[0].hard_cap is True
    assert at_end[0].protected is False


def test_hard_cap_flag_preserved_when_cap_shares_sentence_end():
    # "a.bbbb" (6 chars, no whitespace) with cap 2: the forced split at 2
    # coincides with the sentence end after "a."; SENTENCE kind survives
    # and hard_cap is preserved.
    text = "a.bbbb"
    candidates = extract_candidates(text, max_chars=2)
    at_end = [c for c in candidates if c.end == 2]
    assert len(at_end) == 1
    assert at_end[0].kind == CandidateKind.SENTENCE
    assert at_end[0].hard_cap is True


def test_100k_char_input_is_fast():
    # Guard against catastrophic regex/loop blowup on pathological input
    # (long letter runs, deeply nested delimiters).
    import time

    for text in ("a" * 100_000, "(" * 25_000 + "a" * 25_000 + ")" * 25_000):
        start = time.perf_counter()
        extract_candidates(text, 80)
        assert time.perf_counter() - start < 2.0
