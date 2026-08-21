"""First-push branch detection: before=0000...0 must not crash the diff."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.ci.detect_changes import changed_paths

ZERO = "0" * 40


def test_changed_paths_first_push_lists_initial_commit(tmp_path: Path) -> None:
    """base=0000...0 (first push, no parent) diffs from the empty tree."""
    repo = subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert repo.returncode == 0
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    assert changed_paths(tmp_path, ZERO, head) == ["a.txt"]


def test_changed_paths_normal_range_still_works(tmp_path: Path) -> None:
    """Ordinary base..head ranges are unaffected by the first-push branch."""
    repo = subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert repo.returncode == 0
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    base = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    (tmp_path / "b.txt").write_text("y")
    subprocess.run(["git", "add", "b.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()

    assert changed_paths(tmp_path, base, head) == ["b.txt"]


def test_changed_paths_unresolvable_base_falls_back_to_head(tmp_path: Path) -> None:
    """A base SHA that is not in the clone (force-push rewrote the branch, so
    ``event.before`` is the old dangling head) must not crash the diff: fall
    back to listing the head's files — the conservative "everything affected"
    answer, which runs the full affected CI instead of failing the gate."""
    repo = subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert repo.returncode == 0
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "a.txt").write_text("x")
    subprocess.run(["git", "add", "a.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    # A base that was never in this clone (the pre-force-push dangling head).
    dangling = "a" * 40
    assert changed_paths(tmp_path, dangling, head) == ["a.txt"]
