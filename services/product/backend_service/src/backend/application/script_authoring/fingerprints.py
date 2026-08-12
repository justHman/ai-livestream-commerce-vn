"""Generation fingerprints and approval dependency hashes (tasks 4.4/4.5).

Decision 13: ``GenerationFingerprint`` records enough reproducibility
metadata to explain how an AI draft was produced (model/provider, skill
version/hash, rule-set version/hash, prompt-template version, product-facts
version, promotion version, persona/brief version, generation parameters,
plan version) without storing chain-of-thought.

Decision 14: approval is dependency-bound. ``approval_dependency_hash`` is
the conceptual SHA256 over the exact compiled spoken text, the ordered
selected segment version hashes, the plan version, the rule set, product
facts, promotions, and persona/brief versions. Any bound dependency change
produces a different hash, staling the approval; unrelated metadata never
does (task 4.5).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from backend.application.script_authoring.gate.results import (
    RuleSetFingerprint,
)
from backend.application.script_authoring.models import (
    GenerationFingerprint,
)

__all__ = [
    "approval_dependency_hash",
    "rule_set_version_key",
]

# Stable field labels — the hash is a contract, so labels never change.
_LABEL_SPOKEN_TEXT = "compiled_spoken_text"
_LABEL_SEGMENT_HASH = "segment_version_hash"
_LABEL_PLAN = "plan_version"
_LABEL_RULE_SET = "rule_set"
_LABEL_FACTS = "product_facts"
_LABEL_PROMOTION = "promotion"
_LABEL_PERSONA = "persona_brief"


def rule_set_version_key(
    fingerprint: RuleSetFingerprint | tuple[tuple[str, int], ...] | None,
) -> str:
    """Canonical string key for a rule set (sorted ``id:version`` pairs).

    Accepts a ``RuleSetFingerprint`` or a raw tuple of ``(rule_id,
    version)`` pairs; ``None``/empty yields ``""``. This is the value bound
    into the approval hash (task 4.4: rule set version).
    """
    if fingerprint is None:
        return ""
    pairs = (
        fingerprint.rule_ids
        if isinstance(fingerprint, RuleSetFingerprint)
        else tuple(fingerprint)
    )
    return ",".join(f"{rid}:{ver}" for rid, ver in sorted(pairs))


@dataclass(frozen=True)
class ApprovalDependencies:
    """All values bound into an approval (Decision 14).

    ``spoken_text`` is the exact compiled spoken text; ``segment_hashes``
    are the ordered immutable segment version hashes; the remaining fields
    are the authoritative dependency versions the approval is bound to.
    Unrelated metadata (model id, skill, prompt template, generation
    params) is deliberately NOT part of the approval hash — changing it
    must not stale an approval (task 4.5).
    """

    spoken_text: str
    segment_hashes: tuple[str, ...] = ()
    plan_version: int = 1
    rule_set: str = ""  # canonical rule_set_version_key value
    product_facts_version: str = ""
    promotion_version: str = ""
    persona_brief_version: str = ""


def approval_dependency_hash(deps: ApprovalDependencies) -> str:
    """SHA256 over the approval dependencies (Decision 14).

    Deterministic: identical dependencies -> identical hex digest. Field
    labels are length-prefixed so a permutation of fields cannot collide.
    """
    digest = hashlib.sha256()
    for label, value in (
        (_LABEL_SPOKEN_TEXT, deps.spoken_text),
        (_LABEL_RULE_SET, deps.rule_set),
        (_LABEL_FACTS, deps.product_facts_version),
        (_LABEL_PROMOTION, deps.promotion_version),
        (_LABEL_PERSONA, deps.persona_brief_version),
        (_LABEL_PLAN, str(deps.plan_version)),
    ):
        digest.update(f"{len(label.encode('utf-8'))}:{label}:".encode("utf-8"))
        digest.update(value.encode("utf-8"))
        digest.update(b"\n")
    digest.update(_LABEL_SEGMENT_HASH.encode("utf-8"))
    digest.update(b"\n")
    for segment_hash in deps.segment_hashes:
        digest.update(f"{len(segment_hash.encode('utf-8'))}:".encode("utf-8"))
        digest.update(segment_hash.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()
