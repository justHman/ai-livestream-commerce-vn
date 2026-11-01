"""Immutable typed brief of one ordered part, its provenance and the recap guard.

The brief is written with the plan BEFORE the first write and reused verbatim by
regenerate-part, so a part keeps its intent (a recap stays a recap) instead of being
re-planned from the current facts.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from backend.application.script_authoring.generation.ordered_units import (
    ProductBrief,
    UnitSpec,
    role_title,
)

BRIEF_SCHEMA_VERSION = 1
PLANNER_VERSION = "ordered_units/1"

_SPEECH_ACT = {
    "opening": "introduce",
    "intro": "introduce",
    "highlight": "explain",
    "sizes": "explain",
    "assurance": "reassure",
    "offer": "explain",
    "closing": "close",
}

FALLBACK_REASON_CODES = (
    "coverage_missing",
    "facts_mismatch",
    "unfinished_text",
    "wording_unsuitable",
    "provider_unavailable",
    "time_budget_exhausted",
    "unknown",
)

_RULE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_]+$")


def fact_id(text: str) -> str:
    return "f" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def speech_act(spec: UnitSpec) -> str:
    return "recap" if spec.revisit else _SPEECH_ACT.get(spec.role, "explain")


def facts_revision(product: ProductBrief) -> str:
    raw = "\n".join((*product.claims, "|", *product.prices, "|", *product.discounts))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def build_unit_briefs(specs: list[UnitSpec], product: ProductBrief, new_part_id) -> dict[str, Any]:
    """The plan-level ``unit_briefs`` document; ``parts`` is in script order."""
    revision = facts_revision(product)
    parts = []
    for index, spec in enumerate(specs):
        title = role_title(spec.role).lower()
        parts.append(
            {
                "schema_version": BRIEF_SCHEMA_VERSION,
                "part_id": new_part_id(),
                "role": spec.role,
                "speech_act": speech_act(spec),
                "purpose": f"Nhắc lại {title}" if spec.revisit else role_title(spec.role),
                "required_facts": [
                    {"id": fact_id(c), "text": c, "must_keep": c in spec.must} for c in spec.claims
                ],
                "promos": list(spec.promos),
                "position": {"product_id": product.product_id, "part_index": index},
                "with_cta": spec.with_cta,
                "with_price": spec.with_price,
                "bridge": spec.bridge,
                "first_product": spec.first_product,
                "last_product": spec.last_product,
                "revisit": spec.revisit,
                "target_seconds": spec.target_seconds,
                "facts_revision": revision,
            }
        )
    return {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "planner_version": PLANNER_VERSION,
        "parts": parts,
    }


def spec_from_brief(brief: dict[str, Any]) -> UnitSpec:
    facts = brief.get("required_facts") or []
    return UnitSpec(
        role=brief["role"],
        claims=tuple(f["text"] for f in facts),
        promos=tuple(brief.get("promos") or ()),
        with_price=bool(brief.get("with_price")),
        with_cta=bool(brief.get("with_cta")),
        bridge=bool(brief.get("bridge")),
        first_product=bool(brief.get("first_product")),
        last_product=bool(brief.get("last_product")),
        must=tuple(f["text"] for f in facts if f.get("must_keep")),
        target_seconds=float(brief.get("target_seconds") or 0.0),
        revisit=bool(brief.get("revisit")),
    )


def public_brief(brief: dict[str, Any] | None) -> dict[str, Any] | None:
    if not brief:
        return None
    return {
        "schema_version": brief.get("schema_version", BRIEF_SCHEMA_VERSION),
        "role": brief.get("role"),
        "speech_act": brief.get("speech_act"),
        "purpose": brief.get("purpose"),
    }


def find_brief(unit_briefs: dict[str, Any] | None, part_id: str | None) -> dict[str, Any] | None:
    if not unit_briefs or not part_id:
        return None
    return next((b for b in unit_briefs.get("parts", ()) if b.get("part_id") == part_id), None)


def fallback_reason_code(reasons: list[str] | tuple[str, ...]) -> str:
    """Normalize raw per-attempt reasons into one customer-safe code (the LAST reason decides).

    Raw forms: gate rule ids (``CLAIM_FACTUAL``), guard codes (``coverage``, ``guard:truncated``),
    provider errors (``llm_failed:TimeoutError``) and budget codes. Anything else is ``unknown``.
    """
    for raw in reversed([str(r) for r in reasons]):
        code = _reason_code(raw.strip())
        if code != "unknown":
            return code
    return "unknown"


def _reason_code(raw: str) -> str:
    name = raw.removeprefix("guard:")
    if raw.startswith("llm_failed:"):
        error = raw.split(":", 1)[1]
        return (
            "time_budget_exhausted"
            if "Timeout" in error or "Deadline" in error
            else "provider_unavailable"
        )
    if raw in ("semantic_budget_exhausted",):
        return "time_budget_exhausted"
    if raw in ("model_budget_unavailable", "segment generator returned no result"):
        return "provider_unavailable"
    if name in ("coverage", "missing_info"):
        return "coverage_missing"
    if name == "truncated":
        return "unfinished_text"
    if name in (
        "empty",
        "too_long",
        "foreign_script",
        "personal_experience",
        "hard_sell",
        "bridge",
        "address",
        "cta",
        "mashup",
        "position",
        "name_repeat",
        "brand_repeat",
        "opener",
    ):
        return "wording_unsuitable"
    if _RULE_ID_RE.match(raw):
        return "facts_mismatch" if raw.startswith("CLAIM_") else "wording_unsuitable"
    return "unknown"


def provenance(source: str, reasons: list[str] | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"source": source}
    if source == "fallback":
        out["fallback_reason_code"] = fallback_reason_code(reasons or [])
    return out


# A recap restates what was said; opening like a first-time introduction turns it back into one.
_INTRO_OPENING_RE = re.compile(
    r"^\W*(?:xin\s+)?(?:chào\b|giới thiệu\b|mình\s+(?:xin\s+)?giới thiệu\b|"
    r"hôm nay\s+mình\s+(?:xin\s+)?giới thiệu\b|cùng\s+(?:làm quen|khám phá)\b|"
    r"làm quen\b|lần đầu\b)",
    re.IGNORECASE,
)


def recap_violation(text: str) -> bool:
    """True when a recap part opens as a first-time introduction (deterministic, light)."""
    return bool(_INTRO_OPENING_RE.search(text.strip()))


RECAP_RETRY = (
    "\nBản trước mở đầu như đang giới thiệu lần đầu. Viết lại thành lời NHẮC LẠI: "
    "mở bằng chuyển ý nhắc lại, không chào, không giới thiệu mới."
)
