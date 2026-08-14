"""Architecture guards for the cluster-envelope boundary (tasks 7.2, 7.4).

The Agentic Director consumes the reduced, selected demand (cluster
envelopes, design Decision 9) — NOT the uncontrolled rolling raw comment
list. These guards make that boundary machine-checkable:

- ``assert_no_raw_comment_window_in_prompts``: no ``*.py`` under ``root`` may
  slice/index the raw comment containers (``members``/``rolling_comments``)
  inside ``backend/application/director/`` or ``backend/application/live_runtime/``
  and feed the slice into a prompt (concatenation, f-string embedding, or
  ``" | ".join``). The old Director's ``_answer_prompt``/``_grounded_prompt``
  (``cluster.members[:5]``) are legacy paths scheduled for removal by a later
  cluster, so they remain in the tree; this guard fails the pattern so any
  NEW raw-window-to-prompt code is caught at review time.

- ``assert_no_untrusted_directives_in_prompts``: prompt literals in the same
  two packages must not embed untrusted directive-carrying text without the
  boundary delimiters. Legitimate prompts go through
  ``director.prompts.composer``, which wraps runtime context in
  ``BOUNDARY_BEGIN``/``BOUNDARY_END``; a prompt literal that interpolates a
  ``comment``/``member``/``text`` variable outside those delimiters is
  flagged as an untrusted-directive channel.

Detection is intentionally pragmatic — a regex set over source lines, same
shape as ``live_runtime_guards``/``change_a_contract`` — and deliberately
does not parse Python (a parse would need to prove a slice actually reaches
a prompt; the guard only needs to catch the coupling early).
"""

from __future__ import annotations

import re
from pathlib import Path

from backend.application.script_authoring.change_a_contract import iter_source_files

# The two packages that may build agent prompts from cluster-derived data.
_SCOPED_DIRS = ("director", "live_runtime")

# Raw-window slicing/indexing of the uncontrolled comment containers.
# ``[`` is enough: it also catches ``members[0]`` indexing, not only slices.
_SLICE_MEMBERS = re.compile(r"\.members\[")
_SLICE_ROLLING = re.compile(r"rolling_comments\[")

# Slice immediately fed into prompt concatenation (" | ".join over member
# texts is the classic raw-window-into-prompt pattern).
_JOIN_MEMBERS_SLICE = re.compile(r"\|\s*\.join\(\s*(?:cluster\.)?members\[")
# Slice immediately followed by string concatenation into a prompt.
_CONCAT_MEMBERS_SLICE = re.compile(r"members\[\s*:\s*[^\]]*\]\s*\+")
# f-string prompt embedding a raw member slice directly.
_FSTRING_MEMBERS_SLICE = re.compile(r"f[\"'][^\"']*\{\s*(?:cluster\.)?members\[")

# Prompt literal interpolating untrusted comment/member/text variables
# without the composer's untrusted-context delimiters.
_UNTRUSTED_INTERPOLATION = re.compile(
    r"(?:prompt|system_prompt)\s*=\s*f[\"'][^\"']*\{[^\"']*(?:comment|member|text)[^\"']*\}[\"']"
)
_UNTRUSTED_CONCAT = re.compile(
    r"(?:prompt|system_prompt)\s*=\s*[\"'][^\"']*[\"']\s*\+\s*(?:comment|member|text)"
)
# ``delimiter``-less direct prompt construction; ``_DELIMITED`` proves the
# legitimate composer path carries the untrusted-context delimiters.
_DELIMITED = re.compile(r"(?:UNTRUSTED_CONTEXT_BEGIN|BOUNDARY_BEGIN|BOUNDARY_END)")


def _scoped_files(root: Path) -> list[Path]:
    """``*.py`` files under ``root`` inside the two guarded packages."""
    return [
        path
        for path in iter_source_files(root)
        if any(segment in path.parts for segment in _SCOPED_DIRS)
    ]


def _raw_window_offenders(text: str, relative: str) -> list[str]:
    """Slices of raw comment containers, labeled by how they reach a prompt."""
    offenders: list[str] = []
    if _FSTRING_MEMBERS_SLICE.search(text):
        offenders.append(f"raw comment slice embedded in f-string prompt: {relative}")
    if _CONCAT_MEMBERS_SLICE.search(text):
        offenders.append(f"raw comment slice concatenated into prompt: {relative}")
    if _JOIN_MEMBERS_SLICE.search(text):
        offenders.append(f"raw comment slice joined into prompt: {relative}")
    if _SLICE_MEMBERS.search(text) or _SLICE_ROLLING.search(text):
        offenders.append(f"raw comment container slice in director scope: {relative}")
    return offenders


def assert_no_raw_comment_window_in_prompts(root: Path) -> None:
    """Fail when director/live_runtime code slices raw comment containers.

    Raises ``RuntimeError`` listing offenders when any ``*.py`` file under
    ``root`` inside ``backend/application/director/`` or
    ``backend/application/live_runtime/`` slices or indexes the raw comment
    containers (``members``/``rolling_comments``), or feeds such a slice into
    a prompt via f-string embedding, concatenation, or ``join``.
    """
    offenders: list[str] = []
    for path in _scoped_files(root):
        relative = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        offenders.extend(_raw_window_offenders(text, relative))
    if offenders:
        raise RuntimeError(
            "Raw comment window reaches agent prompt: " + "; ".join(sorted(set(offenders)))
        )


def assert_no_untrusted_directives_in_prompts(root: Path) -> None:
    """Fail when a prompt literal embeds untrusted text without delimiters.

    Raises ``RuntimeError`` listing offenders when any ``*.py`` file under
    ``root`` inside the two guarded packages builds a prompt literal that
    interpolates or concatenates ``comment``/``member``/``text`` variables
    (untrusted directive-carrying content) outside the composer's
    untrusted-context delimiters.
    """
    offenders: list[str] = []
    for path in _scoped_files(root):
        relative = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8")
        if _DELIMITED.search(text):
            continue  # the composer boundary delimiters are present
        if _UNTRUSTED_INTERPOLATION.search(text):
            offenders.append(
                f"untrusted comment text reaches prompt without boundary delimiter: {relative}"
            )
        if _UNTRUSTED_CONCAT.search(text):
            offenders.append(
                f"untrusted comment text reaches prompt without boundary delimiter: {relative}"
            )
    if offenders:
        raise RuntimeError(
            "Untrusted comment text reaches prompt without boundary delimiter: "
            + "; ".join(sorted(set(offenders)))
        )
