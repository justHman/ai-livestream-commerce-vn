"""Deterministic style audit over selected parts of a complete session.

Never changes source text. Exact required spans are exempt, Vietnamese accents
are retained, and sentence/part openings use independent counters.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class VarietyPart:
    key: str
    product_id: str
    role: str
    text: str
    editable: bool = False
    required: tuple[str, ...] = ()


@dataclass(frozen=True)
class Repeat:
    kind: str
    prefix: str
    role: str | None
    members: tuple[str, ...]
    allowance: int

    @property
    def key(self):
        return self.kind, self.prefix, self.role


@dataclass(frozen=True)
class VarietyAudit:
    groups: tuple[Repeat, ...]
    sentence_warnings: tuple[str, ...] = ()

    @property
    def excess(self):
        return sum(len(g.members) - g.allowance for g in self.groups)

    @property
    def keys(self):
        return {g.key for g in self.groups}


def _normalize(text):
    return unicodedata.normalize("NFC", text).lower()


def _prefix(text, offset, exemptions):
    tokens = list(re.finditer(r"[^\W_]+", text, re.UNICODE))[:3]
    if len(tokens) < 3:
        return None
    start, end = offset + tokens[0].start(), offset + tokens[-1].end()
    if any(a <= start and end <= b for a, b in exemptions):
        return None
    words = [t.group() for t in tokens]
    if words[:2] == ["cam", "kết"] or words in (["mình", "cam", "kết"], ["shop", "cam", "kết"]):
        return "cam kết"
    return " ".join(words)


def audit(parts: list[VarietyPart]) -> VarietyAudit:
    by_key = {p.key: p for p in parts}
    if len(by_key) != len(parts):
        raise ValueError("part keys must be unique")
    buckets: dict[tuple[str, str], list[str]] = defaultdict(list)
    local_warnings = []
    for part in parts:
        text = _normalize(part.text)
        exemptions = []
        for required in part.required:
            needle = _normalize(required).strip()
            if needle:
                exemptions.extend(
                    (m.start(), m.end()) for m in re.finditer(re.escape(needle), text)
                )
        first = _prefix(text, 0, exemptions)
        if first:
            buckets["part", first].append(part.key)
        local: dict[str, int] = defaultdict(int)
        for sentence in re.finditer(r"[^.!?…\n]+", text):
            prefix = _prefix(sentence.group(), sentence.start(), exemptions)
            if prefix:
                local[prefix] += 1
        for prefix, count in local.items():
            buckets["sentence", prefix].append(part.key)
            if count >= 2:
                local_warnings.append(part.key)
    groups = []
    for (kind, prefix), members in buckets.items():
        members = tuple(dict.fromkeys(members))
        if len(members) >= 3:
            groups.append(Repeat(kind, prefix, None, members, 2))
        roles: dict[str, list[str]] = defaultdict(list)
        for key in members:
            roles[by_key[key].role].append(key)
        for role, keys in roles.items():
            if role and len(keys) >= 2:
                groups.append(Repeat(kind, prefix, role, tuple(keys), 1))
    return VarietyAudit(tuple(groups), tuple(dict.fromkeys(local_warnings)))


def repair_budget(part_count: int) -> int:
    return min(3, math.ceil(0.2 * part_count))


def repair_candidates(parts: list[VarietyPart], report: VarietyAudit) -> list[VarietyPart]:
    flagged = {key for g in report.groups for key in g.members[g.allowance :]}
    return [p for p in parts if p.editable and p.key in flagged]


def improves(parts: list[VarietyPart], key: str, text: str) -> bool:
    before = audit(parts)
    after = audit([replace(p, text=text) if p.key == key else p for p in parts])
    return after.excess < before.excess and after.keys <= before.keys


def does_not_worsen(parts: list[VarietyPart], key: str, text: str) -> bool:
    before = audit(parts)
    after = audit([replace(p, text=text) if p.key == key else p for p in parts])
    return after.excess <= before.excess and after.keys <= before.keys


_NEUTRAL = frozenset(
    "mình mọi người cả nhà bạn các cùng xem nhé nha với ở phần nói thêm một chút "
    "về riêng còn điểm này tiếp theo mức giá là chỉ nếu nào quan tâm cần hỗ trợ "
    "thì đây chúng ta đến nhìn vào lưu ý hãy cùng nhau xin mời cho cũng có thể".split()
)


def opening_only(original: str, candidate: str) -> bool:
    """Only exchange short neutral lead-ins, preserving sentence bodies and numbers."""

    def sentences(text):
        return [
            re.findall(r"[^\W_]+|[^\w\s]", s)
            for s in re.findall(r"[^.!?…]+[.!?…]*", _normalize(text))
            if s.strip()
        ]

    before, after = sentences(original), sentences(candidate)
    if len(before) != len(after):
        return False
    for old, new in zip(before, after):
        if old == new:
            continue
        tail = 0
        while tail < min(len(old), len(new)) and old[-tail - 1] == new[-tail - 1]:
            tail += 1
        a, b = old[: len(old) - tail], new[: len(new) - tail]
        if tail < 3 or len(a) > 6 or len(b) > 6:
            return False
        if any(token.isalnum() and token not in _NEUTRAL for token in (*a, *b)):
            return False
    return True
