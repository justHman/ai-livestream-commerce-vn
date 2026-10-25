"""Cheap, conservative social-chat filter before any reducer or Director work."""

import re

from backend.application.safety_gate.text_matching import fold_text

_QUESTION = re.compile(
    r"\b(?:gia|bao nhieu|size|mau|ship|con hang|mua|chat lieu|cach|sao|nao|"
    r"gi|may|dau|bao gio|khi nao|khong|ko|khong|k|doi tra|bao hanh|tu van|"
    r"dat hang|chot|lay|xin link|san pham|ao|quan)\b"
)
_SOCIAL = frozenset(
    (
        "ok",
        "oke",
        "okay",
        "okie",
        "u",
        "uh",
        "xinh",
        "hay",
        "hay qua",
        "tuyet",
        "tuyet voi",
        "dep",
        "dep qua",
        "like",
        "tha tim",
        "xin chao",
        "chao",
        "chao shop",
        "hello",
        "hi",
        "cam on",
        "thanks",
        "thank you",
        "wow",
        "yeu qua",
    )
)
_LAUGHTER = re.compile(r"(?:(?:ha|hi|he|ho)+|k{2,}|5{2,}|lol|lmao|huhu)")
_KEYBOARD = re.compile(r"(?:asdf(?:gh(?:jkl)?)?|qwerty(?:uiop)?|zxcv(?:bnm)?)")


def has_question_intent(text: str) -> bool:
    """Question marks or shopping/question vocabulary always keep a safe input eligible."""
    return "?" in text or "？" in text or bool(_QUESTION.search(fold_text(text)))


def is_non_question(text: str) -> bool:
    words = re.findall(r"[^\W_]+", fold_text(text))
    if not words:
        return True
    if all(w.isdigit() for w in words):
        return True
    normalized = " ".join(words)
    if normalized in _SOCIAL:
        return True
    if has_question_intent(text):
        return False
    return all(
        _LAUGHTER.fullmatch(w) or _KEYBOARD.fullmatch(w) or re.fullmatch(r"([a-z])\1{2,}", w)
        for w in words
    )
