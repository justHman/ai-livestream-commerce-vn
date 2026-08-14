"""Optional AI fact-extraction suggestions (tasks 9.5-9.6).

Extraction is a THIN SEAM and NEVER a save dependency: pasted text can always
be saved as a knowledge block without any model call, and a suggestion only
becomes authoritative when the operator explicitly includes it in the next
``PUT /entities/{id}`` (the save path is the single authority — there is no
separate accept endpoint).

The seam degrades silently by design: no LLM configured, malformed output,
or a failing engine all return an empty suggestion list, never a 5xx. The
suggestion list is advisory; the operator edits it in the form.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import Fact
from .registry import Freshness, lookup, resolve_key

logger = logging.getLogger(__name__)

__all__ = [
    "SuggestedFact",
    "SuggestionResponse",
    "coerce_value",
    "fact_from_common",
    "fact_from_row",
    "is_stub_llm",
    "parse_suggestion_json",
    "suggest_facts",
]

# Registry entries a suggestion can legally land on. Suggestion output is
# bounded on purpose: the extraction prompt asks for Vietnamese labels only,
# so suggestions never fill canonical keys the label does not name.
_SUGGESTIBLE_KEYS = frozenset(
    (
        "commerce.price.current",
        "commerce.price.original",
        "commerce.stock.available",
        "commerce.stock.quantity",
        "commerce.promotion",
        "commerce.shipping",
        "commerce.warranty",
        "identity.brand",
        "identity.sku",
    )
)

# True-value strings for boolean facts, Vietnamese included (the registry's
# stock.available labels are "Còn hàng" / "Hết hàng", so operators type
# "có"/"không" or the label itself).
_TRUE_STRINGS = frozenset(("true", "1", "yes", "có"))
_FALSE_STRINGS = frozenset(("false", "0", "no", "không", "hết"))


def is_stub_llm(engine: Any) -> bool:
    """True when the engine is a stub (echo/no-op), not a real model.

    The stub engine (``name == "none"``) echoes the prompt; calling it would
    cost a token budget for zero signal, so the seam short-circuits. ``None``
    (no engine loaded) is also a stub.
    """
    if engine is None:
        return True
    return (getattr(engine, "name", "") or "").lower() in ("none", "", "echo", "stub")


def coerce_value(value: str, fact_type: str, *, key: str) -> Any:
    """Coerce a UI string to the fact's type; ``key`` only for the error text.

    Raises ``ValueError`` for canonical-key mismatches (the registry's type is
    authoritative, so a bad price string is an operator error, not something
    to silently store as text). Custom keys never raise: the caller falls back
    to storing the raw string as ``type="str"``.
    """
    if fact_type == "int":
        return int(value.strip())
    if fact_type == "float":
        return float(value.strip())
    if fact_type == "bool":
        stripped = value.strip().lower()
        if stripped in _TRUE_STRINGS:
            return True
        if stripped in _FALSE_STRINGS:
            return False
        raise ValueError(f"'{value}' is not a boolean value")
    return value


def _custom_type(value: str, type_hint: Optional[str]) -> str:
    """Type for an unknown-label row: prefer a successful coercion, else str.

    The row may carry an explicit ``type`` hint (AI extraction); otherwise the
    value's shape decides (int-looking numbers stay int, decimal strings
    float). Anything uncoercible stores as ``str`` with the raw value.
    """
    if type_hint in ("int", "float", "bool", "str"):
        try:
            coerce_value(value, type_hint, key="custom")
        except ValueError:
            return "str"
        return type_hint
    for candidate in ("int", "float"):
        try:
            coerce_value(value, candidate, key="custom")
        except ValueError:
            continue
        return candidate
    return "str"


def _coerced(value: str, fact_type: str) -> Any:
    """Value after type coercion; never raises for custom keys."""
    try:
        return coerce_value(value, fact_type, key="custom")
    except ValueError:
        return value


def fact_from_row(
    label: str,
    value: str,
    unit: Optional[str],
    *,
    type_hint: Optional[str] = None,
    revision: int = 1,
) -> Fact:
    """Convert one user-facing label/value row to a typed ``Fact``.

    Known labels resolve to canonical keys via the registry (its entry decides
    type, unit and freshness). Unknown labels become ``custom.<slug>`` facts
    and are NEVER rejected for a type mismatch: the value is stored as the
    raw string with ``type="str"`` when coercion fails (Decision 11).
    """
    key = resolve_key(label)
    entry = lookup(key)
    if entry is not None:
        fact_type = entry.type
        # Canonical key: the registry type is authoritative — a price that
        # does not coerce to int is an operator error, not a text fact.
        coerced = coerce_value(value, fact_type, key=key)
        freshness: Freshness = entry.freshness
        entry_unit = entry.unit
    else:
        fact_type = _custom_type(value, type_hint)
        coerced = _coerced(value, fact_type)
        freshness = "stable"
        entry_unit = None
    return Fact(
        key=key,
        type=fact_type,
        value=coerced,
        unit=unit or entry_unit,
        labels=[label],
        revision=revision,
        freshness=freshness,
        source="datastudio",
    )


def fact_from_common(key: str, value: str, *, revision: int = 1) -> Fact:
    """Convert one simple common-field row (``common`` map) to a ``Fact``.

    The key is canonical by construction (the API validates the allowed key
    set), so the registry entry's type/freshness/unit and first label apply;
    a coercion failure is an operator error surfaced by the API layer.
    """
    entry = lookup(key)
    if entry is None:  # pragma: no cover - guarded by the API layer
        raise ValueError(f"unknown common field key '{key}'")
    return Fact(
        key=key,
        type=entry.type,
        value=coerce_value(value, entry.type, key=key),
        unit=entry.unit,
        labels=[entry.labels[0]] if entry.labels else [key],
        revision=revision,
        freshness=entry.freshness,
        source="datastudio",
    )


class SuggestedFact(BaseModel):
    """One extracted suggestion; advisory until the operator saves it."""

    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1, max_length=512)
    unit: Optional[str] = Field(default=None, max_length=64)
    type: Literal["int", "float", "str", "bool"] = "str"


class SuggestionResponse(BaseModel):
    """Advisory extraction result; save path never depends on it (task 9.6)."""

    model_config = ConfigDict(extra="forbid")

    suggestions: list[SuggestedFact] = Field(default_factory=list)
    source_block_id: Optional[str] = None
    note: Optional[str] = None


def parse_suggestion_json(text: str) -> Optional[list[dict]]:
    """Parse a model's JSON-only answer defensively.

    Accepts a bare JSON array, code fences, and leading prose; returns None
    when the output does not contain a JSON array of objects.
    """
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", stripped, re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else stripped
    array_start = candidate.find("[")
    if array_start == -1:
        return None
    try:
        parsed = json.loads(candidate[array_start:])
    except ValueError:
        return None
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return None


def _suggested(row: dict) -> Optional[Fact]:
    """One parsed row -> Fact (skipped silently when the row is unusable)."""
    label = row.get("label")
    value = row.get("value")
    if not isinstance(label, str) or not isinstance(value, str):
        return None
    if not label.strip() or not value.strip():
        return None
    key = resolve_key(label)
    if key not in _SUGGESTIBLE_KEYS:
        return None
    type_hint = row.get("type") if isinstance(row.get("type"), str) else None
    try:
        fact = fact_from_row(
            label,
            value,
            row.get("unit") if isinstance(row.get("unit"), str) else None,
            type_hint=type_hint,
        )
    except ValueError:
        return None
    return fact


def _prompt(text: str, entity_type: str, block_kind: str, block_title: str) -> str:
    """One bounded extraction prompt; JSON array only, max 10 facts."""
    kind_label = block_title.strip() or block_kind
    return (
        "Trích xuất các sự kiện (facts) bán hàng từ văn bản sau. "
        "Trả về MỘT mảng JSON duy nhất, mỗi phần tử có dạng "
        '{"label": "...", "value": "...", "unit": null} — label là nhãn '
        "tiếng Việt người dùng đặt, value là giá trị dạng chuỗi, unit để "
        "null trừ khi có đơn vị rõ ràng. Tối đa 10 phần tử. Chỉ trả về "
        "JSON, không kèm giải thích.\n\n"
        f"Loại thực thể: {entity_type}\n"
        f"Nguồn (kind/title): {kind_label}\n"
        "Văn bản:\n"
        f"{text}"
    )


def _from_engine(
    llm: Any, text: str, entity_type: str, block_kind: str, block_title: str
) -> SuggestionResponse:
    """Blocking extraction through one real LLM engine (never raises)."""
    from llm.engines.base import LLMRequest

    request = LLMRequest.from_prompt(
        _prompt(text, entity_type, block_kind, block_title),
        system_prompt=(
            "Bạn là trợ lý trích xuất dữ liệu bán hàng tiếng Việt. "
            "Luôn trả về JSON thuần, không markdown."
        ),
        max_tokens=512,
        temperature=0,
        stop=["```"],
    )
    response = llm.generate(request)
    parsed = parse_suggestion_json(response.text)
    if parsed is None:
        return SuggestionResponse(note="parse_failed")
    facts = []
    for row in parsed:
        fact = _suggested(row)
        if fact is not None:
            facts.append(
                SuggestedFact(
                    key=fact.key,
                    label=fact.labels[0] if fact.labels else fact.key,
                    value=str(fact.value),
                    unit=fact.unit,
                    type=fact.type,
                )
            )
    return SuggestionResponse(suggestions=facts)


async def suggest_facts(
    llm: Any,
    text: str,
    entity_type: str,
    block_kind: str,
    block_title: str,
) -> SuggestionResponse:
    """Extract fact suggestions from pasted text; never raises, never blocks.

    Returns an empty suggestion list when no real LLM is available, the
    output does not parse, or the engine fails. Extraction is advisory
    (task 9.5); the save path never calls this.
    """
    if is_stub_llm(llm):
        return SuggestionResponse()
    try:
        return await asyncio.to_thread(
            _from_engine, llm, text, entity_type, block_kind, block_title
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "fact extraction failed error_type=%s (suggestions dropped)",
            type(exc).__name__,
        )
        return SuggestionResponse()
