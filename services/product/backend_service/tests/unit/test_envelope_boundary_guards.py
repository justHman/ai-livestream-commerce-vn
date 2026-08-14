"""Task 7.4: guards prove raw comment windows never reach agent prompts.

``envelope_boundary_guards`` fails on the forbidden patterns — slicing raw
comment containers (``members``/``rolling_comments``) in the director or
live_runtime packages, and prompt literals embedding untrusted
directive-carrying text without the composer's boundary delimiters. The
legacy ``_answer_prompt``/``_grounded_prompt`` paths (``cluster.members[:5]``)
stay in the tree until a later cluster removes them, so the guard's negative
tests use SIMULATED offending files (tmp_path) — never the real tree.

The positive test (guard passes on the real source) is in
``test_live_runtime_architecture.py``, which also covers the canonical
chunker guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.application.envelope_boundary_guards import (
    assert_no_raw_comment_window_in_prompts,
    assert_no_untrusted_directives_in_prompts,
)

# tests/unit -> .../backend_service/src
SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ── assert_no_raw_comment_window_in_prompts ───────────────────────────────


def test_raw_window_guard_ignores_benign_join_over_ids(tmp_path: Path) -> None:
    _write(tmp_path, "application/director/state.py", 'ids = " | ".join(cluster.member_ids)\n')

    assert_no_raw_comment_window_in_prompts(tmp_path)


def test_raw_window_guard_fails_on_members_slice(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/director/decision.py",
        'joined = " | ".join(cluster.members[:5])\n',
    )

    with pytest.raises(RuntimeError, match="Raw comment window reaches agent prompt"):
        assert_no_raw_comment_window_in_prompts(tmp_path)


def test_raw_window_guard_fails_on_qualified_members_slice(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/director/decision.py",
        'joined = " | ".join(top.cluster.members[:5])\n',
    )

    with pytest.raises(RuntimeError, match="Raw comment window reaches agent prompt"):
        assert_no_raw_comment_window_in_prompts(tmp_path)


def test_raw_window_guard_fails_on_members_indexing(tmp_path: Path) -> None:
    _write(tmp_path, "application/live_runtime/prompter.py", "text = cluster.members[0]\n")

    with pytest.raises(RuntimeError, match="Raw comment window reaches agent prompt"):
        assert_no_raw_comment_window_in_prompts(tmp_path)


def test_raw_window_guard_fails_on_rolling_comments_slice(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/director/decision.py",
        'prompt = " | ".join(state.rolling_comments[:10])\n',
    )

    with pytest.raises(RuntimeError, match="Raw comment window reaches agent prompt"):
        assert_no_raw_comment_window_in_prompts(tmp_path)


def test_raw_window_guard_fails_on_fstring_member_slice(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/live_runtime/prompter.py",
        'prompt = f"comments: {cluster.members[:5]}"\n',
    )

    with pytest.raises(RuntimeError, match="Raw comment window reaches agent prompt"):
        assert_no_raw_comment_window_in_prompts(tmp_path)


def test_raw_window_guard_fails_on_slice_concatenation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/director/decision.py",
        'prompt = "x" + cluster.members[:5] + "y"\n',
    )

    with pytest.raises(RuntimeError, match="Raw comment window reaches agent prompt"):
        assert_no_raw_comment_window_in_prompts(tmp_path)


def test_raw_window_guard_ignores_files_outside_guarded_packages(tmp_path: Path) -> None:
    _write(tmp_path, "application/other/prompter.py", 'x = " | ".join(cluster.members[:5])\n')

    assert_no_raw_comment_window_in_prompts(tmp_path)


# ── assert_no_untrusted_directives_in_prompts ─────────────────────────────


def test_untrusted_guard_passes_on_current_source_tree() -> None:
    assert_no_untrusted_directives_in_prompts(SRC_ROOT)


def test_untrusted_guard_ignores_delimited_composer_prompt(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/director/prompts/composer.py",
        'prompt = f"static {comment}"\nBOUNDARY_BEGIN = "<<<UNTRUSTED_CONTEXT_BEGIN>>>"\n',
    )

    assert_no_untrusted_directives_in_prompts(tmp_path)


def test_untrusted_guard_fails_on_fstring_comment_interpolation(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/director/decision.py",
        'prompt = f"Answer: {comment.text}"\n',
    )

    with pytest.raises(RuntimeError, match="without boundary delimiter"):
        assert_no_untrusted_directives_in_prompts(tmp_path)


def test_untrusted_guard_fails_on_concat_comment(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/live_runtime/prompter.py",
        'prompt = "Reply: " + member\n',
    )

    with pytest.raises(RuntimeError, match="without boundary delimiter"):
        assert_no_untrusted_directives_in_prompts(tmp_path)


def test_untrusted_guard_ignores_prompt_without_untrusted_variables(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "application/director/decision.py",
        'prompt = f"stage {stage} instruction {instruction}"\n',
    )

    assert_no_untrusted_directives_in_prompts(tmp_path)


def test_untrusted_guard_ignores_files_outside_guarded_packages(tmp_path: Path) -> None:
    _write(tmp_path, "application/other/prompter.py", 'prompt = f"Answer: {comment.text}"\n')

    assert_no_untrusted_directives_in_prompts(tmp_path)
