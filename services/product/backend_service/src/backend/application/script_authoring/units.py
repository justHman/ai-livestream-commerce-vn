"""Script units: the ordered spoken parts of one approved product text.

A unit is one blank-line separated paragraph (the compile joiner is a blank
line). The split is a pure function of the text, so approval hashes and the
runtime envelope fingerprint need no extra field and can never drift from the
units the Director plays. The gate and the runtime MUST use this one splitter.
"""

from __future__ import annotations

import re

__all__ = ["split_units"]

_BLANK_LINE = re.compile(r"\r?\n[ \t\r]*\n")


def split_units(text: str) -> tuple[str, ...]:
    """Non-empty, stripped paragraphs of ``text`` (exact substrings of it)."""
    return tuple(part.strip() for part in _BLANK_LINE.split(text) if part.strip())
