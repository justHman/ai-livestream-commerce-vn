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


def has_question_intent(text: str) -> bool:
    """Question marks or shopping/question vocabulary always keep a safe input eligible."""
    return "?" in text or "？" in text or bool(_QUESTION.search(fold_text(text)))


def is_non_question(text: str) -> bool:
    if has_question_intent(text):
        return False
    words = re.findall(r"[a-z0-9]+", fold_text(text))
    if not words:
        return True
    normalized = " ".join(words)
    if normalized in _SOCIAL:
        return True
    return all(_LAUGHTER.fullmatch(w) or re.fullmatch(r"([a-z])\1{2,}", w) for w in words)
