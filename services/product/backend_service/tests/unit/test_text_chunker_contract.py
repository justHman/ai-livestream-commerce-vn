"""Regression tests for adaptive speech-text chunking (tasks 2.5-2.6).

Section 2.5 validates the configuration contract (positive ordered character
thresholds, non-negative timeout, clear ValueError messages) and pins the
deterministic fallback role of ``target_chars``: the setting must not be
dead under the fixed policy. Section 2.6 locks the canonical ``TextChunk``
identity contract.

Intended-failure map on the current baseline (HEAD 62eddfa):
  - min_chars=0 / target_chars=0 / max_chars=0 currently pass construction
    (only the ordering and flush_timeout_ms guards exist), so all
    zero-threshold tests are INTENDED RED. The ordering guard
    (min <= target <= max) and flush_timeout_ms >= 0 guard already exist
    and stay green.
  - test_target_chars_changes_fixed_fallback_boundary: INTENDED RED. The
    baseline chunker only consults ``target_chars`` to validate ordering;
    two runs differing only in target_chars produce identical chunks.
  - test_canonical_render_windows_textchunk_is_speech_chunking_textchunk:
    INTENDED RED. ``render.windows`` still defines its own ``TextChunk``
    instead of re-exporting the canonical class.
  - test_render_textchunk_supports_metadata_fields: INTENDED RED for the
    same reason. Once the migration lands, the canonical ``TextChunk``
    (with ``decision_reason``) must remain compatible with every metadata
    field the current ``render.windows.TextChunk`` carries.
  - test_no_source_field_on_public_chunk_types: GREEN (canonical type has
    no llm/script/source/producer field); kept as a contract guard.
  - test_no_duplicate_textchunk_class_in_backend_application: INTENDED RED.
    The baseline defines ``TextChunk`` in both ``render/windows.py`` and
    ``speech_chunking/types.py``.
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
    with pytest.raises(ValueError, match="flush_timeout_ms"):
        TextChunker(session_id="s", utterance_id="u", flush_timeout_ms=-1)


def test_zero_flush_timeout_is_accepted() -> None:
    # flush_timeout_ms >= 0 per spec: zero is a legal non-negative value.
    TextChunker(session_id="s", utterance_id="u", flush_timeout_ms=0)


# ---------- 2.5 target_chars deterministic fallback role ----------

# One sentence with no internal punctuation, safely under the hard cap, so
# the only split signal under the fixed policy is the fallback target.
TARGET_PROBE_TEXT = "Xin chào quý khách hàng thân mến hôm nay"


def test_target_chars_changes_fixed_fallback_boundary() -> None:
    """Two runs differing ONLY in target_chars must pick different boundaries.

    INTENDED RED on baseline: the chunker never consults target_chars
    beyond ordering validation, so the runs produce identical chunk texts.
    The implementer must give target_chars a deterministic fixed fallback
    role (spec "Target character fallback").
    """
    text = TARGET_PROBE_TEXT
    probe = TextChunker(session_id="s", utterance_id="u")
    probe.feed(text)
    assert probe.buffered_text == text

    low = _segment([text], min_chars=6, target_chars=12, max_chars=40)
    high = _segment([text], min_chars=6, target_chars=36, max_chars=40)
    assert "".join(low) == text
    assert "".join(high) == text
    assert low != high


def test_target_chars_equals_probe_text_length_is_whole_chunk() -> None:
    """When the fallback target exactly matches the pending text length, the
    whole probe is one chunk; a much smaller target splits it."""
    text = TARGET_PROBE_TEXT
    whole = _segment([text], min_chars=6, target_chars=len(text), max_chars=len(text))
    assert whole == [text]


# ---------- 2.5 target fallback: boundary and fragmentation invariance ----------


def test_target_fallback_splits_at_whitespace_immediately_above_target() -> None:
    """Whitespace immediately above target_chars wins, stamped FIXED_FALLBACK.

    The decision horizon (len >= max_chars) scans whitespace on BOTH sides of
    target_chars. With whitespace at target+1 (13) and a far whitespace at 38,
    the boundary must land at 13 with fixed_fallback — a downward-only scan
    would land at 38 with hard_max, so this pins the both-sides behavior and
    the decision reason.
    """
    probe = "a" * 12 + " " + "a" * 24 + " " + "a" * 2  # len 40 = max_chars
    chunker = TextChunker(
        session_id="s", utterance_id="u", min_chars=6, target_chars=12, max_chars=40
    )

    emitted: list[TextChunk] = []
    emitted.extend(chunker.feed(probe))
    emitted.extend(chunker.finalize())

    assert emitted[0].text == probe[:13]
    assert emitted[0].decision_reason == "fixed_fallback"
    assert "".join(chunk.text for chunk in emitted) == probe


def test_target_fallback_fragmentation_invariance() -> None:
    """One full feed and char/word fragments produce identical chunk texts.

    The fallback split position depends only on the accumulated buffer text
    (whitespace nearest target on both sides), never on how the buffer was
    fed, so fragmentation cannot shift the boundary. The only whitespace sits
    exactly at target_chars (15), so the head is exactly target-length in all
    three fragmentations.
    """
    text = "abcdefghijklmn opqrstuvwxyzabcdefghijklmnop"  # space at 15, len 41
    params = dict(min_chars=6, target_chars=15, max_chars=40)

    whole = _segment([text], **params)

    words = text.split(" ")
    word_frags = [w + " " for w in words[:-1]] + [words[-1]]
    by_words = _segment(word_frags, **params)

    by_chars = _segment(list(text), **params)

    assert "".join(whole) == text
    assert whole == by_words == by_chars
    assert whole[0] == text[:15]


# ---------- 2.6 canonical TextChunk identity and compatibility ----------


def test_canonical_render_windows_textchunk_is_speech_chunking_textchunk() -> None:
    """Legacy ``render.windows.TextChunk`` must BE the canonical type.

    INTENDED RED until task 2.6 migrates ``render.windows.TextChunk`` to
    re-export the canonical class.
    """
    from backend.application.render.windows import TextChunk as RenderTextChunk
    from backend.application.speech_chunking import TextChunk as CanonicalTextChunk

    assert CanonicalTextChunk is RenderTextChunk


def test_render_textchunk_supports_metadata_fields() -> None:
    """The canonical type must accept every constructor field the current
    render TextChunk carries, plus the canonical metadata (decision_reason).

    INTENDED RED until task 2.6 lands; then the canonical dataclass (with
    ``decision_reason``) must remain compatible with positional and keyword
    construction.
    """
    from backend.application.render.windows import TextChunk as RenderTextChunk
    from backend.application.speech_chunking import TextChunk as CanonicalTextChunk

    assert CanonicalTextChunk is RenderTextChunk
    instance = RenderTextChunk(
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

    from backend.application.speech_chunking import TextChunk as CanonicalTextChunk

    field_names = {f.name for f in fields(CanonicalTextChunk)}
    assert not {"llm", "script", "source", "producer"} & field_names


# ---------- 2.6 duplicate TextChunk definition ----------


def test_no_duplicate_textchunk_class_in_backend_application() -> None:
    """Exactly one ``TextChunk`` class definition may exist in the backend
    application package; ``render.windows`` and ``text_chunker`` must re-export
    the canonical class, not re-declare it.

    INTENDED RED on baseline: ``render/windows.py`` and
    ``speech_chunking/types.py`` both define the class.
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
