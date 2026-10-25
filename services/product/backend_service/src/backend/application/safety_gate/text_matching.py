"""Matching-only Vietnamese views. Never replace the viewer's original text."""

import re
import unicodedata


def fold_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower().replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


# Accent folding otherwise turns these common commerce words into abusive tokens.
# Only exact accented words are protected; unaccented abuse remains matchable.
_BENIGN_HOMOGRAPHS = frozenset("ngủ lớn đủ các buổi dẻo khung bảo dù dư đi".split())
_BENIGN_HOMOGRAPHS |= frozenset("\u0111\u1ec1u \u0111i\u1ec7n h\u00e0m".split())
_LEET = str.maketrans({"1": "i", "0": "o", "3": "e", "@": "a", "$": "s"})


def matching_view(text: str, *, join_symbols: bool = False) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"\w+", lambda m: "benignword" if m[0] in _BENIGN_HOMOGRAPHS else m[0], text)
    text = fold_text(text).translate(_LEET)
    text = re.sub(r"\bd[*!]t\b", "dit", text)
    if join_symbols:
        text = re.sub(r"(?<=[a-z])[^\w\s]+(?=[a-z])", "", text)
        text = re.sub(r"(?<=[a-z])_+(?=[a-z])", "", text)
    tokens = re.findall(r"[a-z0-9]+", text)
    result: list[str] = []
    for token in tokens:
        token = re.sub(r"([a-z])\1{2,}", r"\1", token)
        # Elongation with separated final letters: 'ngu uuu', 'ngu u u'.
        if result and len(result[-1]) >= 2 and set(token) == {result[-1][-1]}:
            continue
        result.append(token)
    if join_symbols:
        # Only runs of single letters, never arbitrary neighbouring words.
        return re.sub(
            r"(?<!\S)(?:[a-z] ){1,3}[a-z](?!\S)",
            lambda m: m[0].replace(" ", ""),
            " ".join(result),
        )
    return " ".join(result)
