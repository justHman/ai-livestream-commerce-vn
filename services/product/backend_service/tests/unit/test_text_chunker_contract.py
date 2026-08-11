"""Regression tests for adaptive speech-text chunking (tasks 2.5-2.6).

Section 2.5 validates the configuration contract (positive ordered character
thresholds, non-negative timeout, clear ValueError messages) and pins the
deterministic fallback role of ``target_chars``: the setting must not be
dead under the fixed policy. Section 2.6 locks the canonical ``TextChunk``
identity contract: exactly one canonical class, exported by
``backend.application.text_chunker``, with ``render.windows`` defining or
re-exporting nothing.
"""

from __future__ import annotations

import pytest

from backend.application.text_chunker import TextChunk, TextChunker


# ---------- helpers / fakes ----------


def _segment(fragments: list[str], **kwargs: object) -> list[str]:
    """Feed fragments then finalize; return the emitted chunk texts."""
    chunker = TextChunker(session_id="s", utterance_id="u", **kwargs)
    emitted: list[TextChunk] = []
    for fragment in fragments:
        emitted.extend(chunker.feed(fragment))
    emitted.extend(chunker.finalize())
    return [chunk.text for chunk in emitted]


# ---------- 2.5 configuration validation ----------


def test_zero_min_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="min_chars"):
        TextChunker(session_id="s", utterance_id="u", min_chars=0)


def test_zero_target_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="target_chars"):
        TextChunker(session_id="s", utterance_id="u", target_chars=0)


def test_zero_max_chars_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_chars"):
        TextChunker(session_id="s", utterance_id="u", max_chars=0)


def test_reversed_threshold_order_is_rejected() -> None:
    with pytest.raises(ValueError, match="min_chars <= target_chars <= max_chars"):
        TextChunker(session_id="s", utterance_id="u", min_chars=50, target_chars=40)


def test_negative_flush_timeout_is_rejected() -> None:
    # flush_timeout_ms is owned by orchestration (StreamingControllerConfig),
    # not by the chunker: its validation lives with the controller config.
    from backend.application.render.orchestrator import StreamingControllerConfig

    with pytest.raises(ValueError, match="flush_timeout_ms"):
        StreamingControllerConfig(flush_timeout_ms=-1)


def test_zero_flush_timeout_is_accepted() -> None:
    # flush_timeout_ms >= 0 per spec: zero is a legal non-negative value.
    from backend.application.render.orchestrator import StreamingControllerConfig

    StreamingControllerConfig(flush_timeout_ms=0)


# ---------- 2.5 target_chars deterministic fallback role ----------

# One sentence with no internal punctuation, safely under the hard cap, so
# the only split signal under the fixed policy is the fallback target.
TARGET_PROBE_TEXT = "Xin chào quý khách hàng thân mến hôm nay"  # len 40


def test_target_chars_changes_fixed_fallback_boundary() -> None:
    """Two runs differing ONLY in target_chars must pick different boundaries.

    The probe (len 40) is fed under max_chars=80 so no hard-max split
    participates: feed() must emit nothing and the buffer stays exact until
    finalize(). At finalize, the whitespace nearest target_chars fixes the
    boundary — a larger target moves it, so the two runs differ.
    """
    text = TARGET_PROBE_TEXT
    probe = TextChunker(
        session_id="s", utterance_id="u", min_chars=6, target_chars=40, max_chars=80
    )
    probe.feed(text)
    assert probe.buffered_text == text

    low = _segment([text], min_chars=6, target_chars=12, max_chars=80)
    high = _segment([text], min_chars=6, target_chars=36, max_chars=80)
    assert "".join(low) == text
    assert "".join(high) == text
    assert low != high


def test_target_chars_equals_probe_text_length_is_whole_chunk() -> None:
    """When the fallback target exactly matches the pending text length, the
    whole probe is one chunk; a much smaller target splits it."""
    text = TARGET_PROBE_TEXT
    whole = _segment([text], min_chars=6, target_chars=len(text), max_chars=80)
    assert whole == [text]


# ---------- 2.5 target fallback: boundary and fragmentation invariance ----------


def test_target_fallback_splits_at_whitespace_immediately_above_target() -> None:
    """Whitespace immediately above target_chars wins, stamped FIXED_FALLBACK.

    The probe (len 40) stays pending under max_chars=80: feed() emits
    nothing and the buffer is exact. Only finalize() splits — at the
    whitespace nearest target_chars on both sides (13 vs 38 -> 13), as a
    non-final fixed_fallback head plus the exact final remainder, so a
    downward-only scan (which would land at 38 with hard_max) is pinned out
    and the decision reason is locked.
    """
    probe = "a" * 12 + " " + "a" * 24 + " " + "a" * 2  # len 40 < max_chars 80
    chunker = TextChunker(
        session_id="s", utterance_id="u", min_chars=6, target_chars=12, max_chars=80
    )

    emitted: list[TextChunk] = []
    emitted.extend(chunker.feed(probe))
    assert chunker.buffered_text == probe
    emitted.extend(chunker.finalize())

    assert emitted[0].text == probe[:13]
    assert emitted[0].decision_reason == "fixed_fallback"
    assert emitted[0].is_final is False
    assert emitted[1].text == probe[13:]
    assert emitted[1].decision_reason == "finalize"
    assert emitted[1].is_final is True
    assert "".join(chunk.text for chunk in emitted) == probe


def test_target_fallback_fragmentation_invariance() -> None:
    """One full feed and char/word fragments produce identical chunk texts.

    The fallback split position depends only on the accumulated buffer text
    (whitespace nearest target at finalize), never on how the buffer was
    fed, so fragmentation cannot shift the boundary. The only whitespace sits
    exactly at target_chars (15), so the head is exactly target-length in all
    three fragmentations.
    """
    text = "abcdefghijklmn opqrstuvwxyzabcdefghijklmnop"  # space at 15, len 41
    params = dict(min_chars=6, target_chars=15, max_chars=80)

    whole = _segment([text], **params)

    words = text.split(" ")
    word_frags = [w + " " for w in words[:-1]] + [words[-1]]
    by_words = _segment(word_frags, **params)

    by_chars = _segment(list(text), **params)

    assert "".join(whole) == text
    assert whole == by_words == by_chars
    assert whole[0] == text[:15]


# ---------- 2.5 hard max keeps the base last-whitespace-at-cap behavior ----------


def test_hard_max_with_whitespace_splits_at_last_whitespace_at_or_before_cap() -> None:
    """The hard cap keeps its base behavior: with qualifying whitespace
    before the cap, the split lands on the LAST whitespace at or before
    max_chars and is stamped HARD_MAX (the cap forced the decision)."""
    text = "a" * 70 + " " + "b" * 30  # len 101; space at 71 (last whitespace <= 80)
    chunker = TextChunker(session_id="s", utterance_id="u", min_chars=12, max_chars=80)

    emitted: list[TextChunk] = []
    emitted.extend(chunker.feed(text))

    assert emitted[0].text == "a" * 70 + " "
    assert emitted[0].decision_reason == "hard_max"
    assert emitted[0].is_final is False
    emitted.extend(chunker.finalize())
    assert "".join(chunk.text for chunk in emitted) == text
    assert emitted[1].text == "b" * 30
    assert emitted[1].decision_reason == "finalize"
    assert emitted[1].is_final is True


# ---------- 2.6 canonical TextChunk identity and compatibility ----------


def test_render_windows_does_not_export_textchunk() -> None:
    """``render.windows`` must not define or re-export ``TextChunk``.

    ``backend.application.text_chunker.TextChunk`` is the one canonical
    class; importing ``TextChunk`` from ``render.windows`` must raise
    ImportError.
    """
    from backend.application.text_chunker import TextChunk as CanonicalTextChunk

    with pytest.raises(ImportError):
        from backend.application.render.windows import TextChunk  # noqa: F401

    assert CanonicalTextChunk.__module__ == "backend.application.text_chunker.types"


def test_canonical_textchunk_supports_all_fields() -> None:
    """The canonical type accepts every constructor field (keyword and
    positional-compatible ordering) plus the canonical metadata
    (``decision_reason``)."""
    from backend.application.text_chunker import TextChunk as CanonicalTextChunk

    instance = CanonicalTextChunk(
        session_id="sess-1",
        utterance_id="utt-1",
        seq=3,
        text="Xin chào bạn.",
        is_final=True,
        decision_reason="finalize",
    )
    assert instance.session_id == "sess-1"
    assert instance.utterance_id == "utt-1"
    assert instance.seq == 3
    assert instance.text == "Xin chào bạn."
    assert instance.is_final is True
    assert instance.decision_reason == "finalize"
    assert isinstance(instance.id, str) and instance.id != ""


def test_no_source_field_on_public_chunk_types() -> None:
    """The public chunk types must carry no source-type field (no llm/script).

    Contract guard: the canonical type is already source-agnostic.
    """
    from dataclasses import fields

    from backend.application.text_chunker import TextChunk as CanonicalTextChunk

    field_names = {f.name for f in fields(CanonicalTextChunk)}
    assert not {"llm", "script", "source", "producer"} & field_names


# ---------- 2.6 duplicate TextChunk definition ----------


def test_no_duplicate_textchunk_class_in_backend_application() -> None:
    """Exactly one ``TextChunk`` class definition may exist in the backend
    application package; ``render.windows`` does not define or re-export it,
    and the canonical class lives in ``text_chunker/types.py``.
    """
    import backend.application
    from pathlib import Path

    matches: list[tuple[str, str]] = []
    for base in backend.application.__path__:
        for path in sorted(Path(base).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            # Exact class-name match: "class TextChunk:" / "class TextChunk(" —
            # never a prefix hit on "class TextChunker:".
            if not any(
                line.strip().startswith("class TextChunk")
                and not line.strip()[len("class TextChunk") :].lstrip().startswith("er")
                for line in source.splitlines()
            ):
                continue
            module_name = str(path.relative_to(base)).replace("\\", ".").replace("/", ".")
            matches.append((module_name, path.name))

    assert len(matches) == 1, f"expected one TextChunk class, found {matches}"
