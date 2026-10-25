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
_BENIGN_HOMOGRAPHS |= frozenset("bộ mã máy".split())
_LEET = str.maketrans({"1": "i", "0": "o", "3": "e", "@": "a", "$": "s"})
_AMBIGUOUS = frozenset("ngu lon cac du buoi deo bui dien cut ham deu khung lao hon".split())
_EXPLICIT_ABUSE = frozenset("lồn lờn cặc kặc đụ đù buồi đéo điên cứt hâm đểu khùng láo hỗn".split())

# These folded phrases also name ordinary goods/requests. They still use the
# original protected view; longer, unambiguous curated phrases match first.
AMBIGUOUS_PHRASES = frozenset(
    {
        "do ngu",
        "con ngu",
        "do dien",
        "du ma",
        "con cac",
        "cai lon",
        "cai buoi",
        "vai lon",
        "vai ca lon",
        "vai cac",
        "vai buoi",
        "con di",
        "di me",
        "di diem",
        "cho de",
        "bo may",
        "ma may",
        "bo lao",
        "do khon",
        "con khon",
        "thang khon",
        "do khi",
    }
)


def _protect_commerce_homophones(text: str) -> str:
    """Require chó's accent; khi/khon singletons need accents or targeting.

    These occur inside longer curated phrases too (do cho ma, nao cho), so
    protecting only whole ambiguous phrases would still ban ordinary requests.
    """
    words = re.findall(r"\w+", text)
    context = " ".join(words)
    targeted = any(w in {"mày", "tao", "thằng"} for w in words)
    targeted |= bool(
        re.search(
            r"\b(?:may\s+(?:la|dung|an|ngu|dien)|(?:cho|khon|khi)\s+may|"
            r"thang\s+(?:cho|khon|khi)(?!\s+(?:be|em|me|con|chong|vo)\b))\b",
            context,
        )
    )
    targeted |= bool(re.search(r"\b(?:đúng là|dung la|cái thứ|cai thu|cái đồ|cai do)\b", context))
    if not targeted:
        # A machine set/code and workwear must not become bố/má mày or bố láo.
        text = re.sub(r"\b(?:bo may|ma may|bo lao)\b", "benignword", text)

    def replace(match: re.Match) -> str:
        token = match[0]
        if fold_text(token) not in {"cho", "khi", "khon"}:
            return token
        if fold_text(token) == "cho":
            # An address never turns merchandise 'cho' into the insult 'chó'.
            return token if token == "chó" else "benignword"
        if token in {"khỉ", "khốn"}:
            return token
        # Explicit benign accents are meaningful even in a hostile sentence.
        return token if targeted and token == fold_text(token) else "benignword"

    return re.sub(r"\w+", replace, text)


def _restore_accented_cho(view: str) -> str:
    # Symbol joins must not recreate an ASCII cho after homophone protection.
    view = re.sub(r"\bcho\b", "benignword", view)
    return view.replace("markedchotoken", "cho")


def phrase_matching_view(text: str, *, join_symbols: bool, reduce_repeats: bool) -> str:
    """Keep curated phrase context before protecting ambiguous ASCII singletons.

    Explicit benign accents still carry meaning (cái lớn != cái lồn). 'đi' is
    a valid part of curated dismissal phrases such as 'cút mẹ đi'.
    """
    text = unicodedata.normalize("NFC", text).lower()
    text = re.sub(r"\bchó\b", "markedchotoken", text)
    # Multiword evidence is checked before ambiguous singleton protection.
    # Preserve meaningful benign accents, and require the actual abusive accent
    # for cho/khi phrases: ASCII 'do cho' must never become 'đồ chó'.
    text = re.sub(
        r"\w+",
        lambda m: (
            "benignword"
            if (fold_text(m[0]) in {"cho", "khi"} and m[0] not in {"chó", "khỉ"}) or m[0] == "khôn"
            else m[0]
        ),
        text,
    )
    text = re.sub(
        r"\w+",
        lambda m: "benignword" if m[0] in _BENIGN_HOMOGRAPHS - {"đi"} else m[0],
        text,
    )
    return _restore_accented_cho(
        matching_view(
            text, join_symbols=join_symbols, protect_homographs=False, reduce_repeats=reduce_repeats
        )
    )


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
        text = re.sub(r"\bchó\b", "markedchotoken", text)
        text = _protect_commerce_homophones(text)
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
        view = re.sub(
            r"(?<!\S)(?:[a-z] ){1,3}[a-z](?!\S)",
            lambda m: m[0].replace(" ", ""),
            " ".join(result),
        )
    else:
        view = " ".join(result)
    return _restore_accented_cho(view) if protect_homographs else view
