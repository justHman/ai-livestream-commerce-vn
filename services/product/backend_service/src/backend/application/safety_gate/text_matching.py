"""Matching-only Vietnamese views. Never replace the viewer's original text."""

import re
import unicodedata


def fold_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text).lower().replace("đ", "d")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


# Accent folding otherwise turns these common commerce words into abusive tokens.
# Original accents take precedence; ambiguous ASCII needs additional evidence.
_BENIGN_HOMOGRAPHS = frozenset(
    "ngủ ngữ lớn đủ các buổi dẻo đeo khung bảo dù dư đi bùi đào dạo".split()
)
_BENIGN_HOMOGRAPHS |= frozenset("\u0111\u1ec1u \u0111i\u1ec7n h\u00e0m".split())
_LEET = str.maketrans({"1": "i", "0": "o", "3": "e", "@": "a", "$": "s"})
_AMBIGUOUS = frozenset("ngu lon cac du buoi deo bui dien cut ham deu khung lao hon".split())
_EXPLICIT_ABUSE = frozenset("lồn lờn cặc kặc đụ đù buồi đéo điên cứt hâm đểu khùng láo hỗn".split())


def _protect_homographs(text: str) -> str:
    words = list(re.finditer(r"\w+", text))
    positions = {word.start(): index for index, word in enumerate(words)}

    def replace(match: re.Match) -> str:
        token = match[0]
        if token in _BENIGN_HOMOGRAPHS:
            return "benignword"
        folded = fold_text(token)
        reduced = re.sub(r"([a-z])\1+", r"\1", folded)
        original_reduced = re.sub(r"(.)\1+", r"\1", token)
        if reduced not in _AMBIGUOUS or original_reduced in _EXPLICIT_ABUSE:
            return token
        # Accents carry meaning. Do not reinterpret a benign accented word
        # through a folded profanity, even when other words sound hostile.
        if token != folded:
            return "benignword"
        index = positions[match.start()]
        before = words[index - 1][0] if index else ""
        after = words[index + 1][0] if index + 1 < len(words) else ""
        insulting = before in {"đồ", "thằng", "thang", "mày", "may", "tao", "đm", "dm"}
        insulting |= after in {"mày", "may", "quá", "qua"} and reduced == "ngu"
        elongated = reduced == "ngu" and (
            folded != reduced or (after and set(after) == {"u"}) or len(words) == 1
        )
        return token if insulting or elongated else "benignword"

    return re.sub(r"\w+", replace, text)


def matching_view(
    text: str,
    *,
    join_symbols: bool = False,
    protect_homographs: bool = True,
    reduce_repeats: bool = False,
) -> str:
    text = unicodedata.normalize("NFC", text).lower()
    if protect_homographs:
        text = _protect_homographs(text)
    text = fold_text(text).translate(_LEET)
    text = re.sub(r"\bd[*!]t\b", "dit", text)
    if join_symbols:
        text = re.sub(r"(?<=[a-z])[^\w\s]+(?=[a-z])", "", text)
        text = re.sub(r"(?<=[a-z])_+(?=[a-z])", "", text)
    tokens = re.findall(r"[a-z0-9]+", text)
    result: list[str] = []
    for token in tokens:
        if reduce_repeats:
            token = re.sub(r"([a-z])\1+", r"\1", token)
        else:
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
