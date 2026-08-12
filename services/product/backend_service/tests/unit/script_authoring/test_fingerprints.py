"""Tasks 4.4/4.5 tests: approval dependency hash sensitivity.

Any bound dependency change (text edit, segment version change,
product-facts/promotion/persona/plan/rule-set version change) MUST change
the approval hash; unrelated metadata (model, skill, prompt template,
generation params) MUST NOT.
"""

from __future__ import annotations

from backend.application.script_authoring.fingerprints import (
    ApprovalDependencies,
    approval_dependency_hash,
    rule_set_version_key,
)
from backend.application.script_authoring.gate.results import RuleSetFingerprint


def _deps(**overrides) -> ApprovalDependencies:
    base = dict(
        spoken_text="Kem ABC chỉ hai trăm chín mươi chín nghìn đồng.",
        segment_hashes=("hash-seg-1", "hash-seg-2"),
        plan_version=3,
        rule_set="CLAIM_PRICE:2,FORMAT_CONTROL:1",
        product_facts_version="facts-v5",
        promotion_version="promo-v2",
        persona_brief_version="persona-v1",
    )
    base.update(overrides)
    return ApprovalDependencies(**base)


def test_text_edit_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(
        _deps(spoken_text="Kem ABC chỉ hai trăm chín mươi chín nghìn đồng!")
    )


def test_segment_version_change_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(
        _deps(segment_hashes=("hash-seg-1", "hash-seg-2-NEW"))
    )


def test_segment_order_change_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(
        _deps(segment_hashes=("hash-seg-2", "hash-seg-1"))
    )


def test_product_facts_version_change_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(
        _deps(product_facts_version="facts-v6")
    )


def test_promotion_version_change_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(
        _deps(promotion_version="promo-v3")
    )


def test_persona_version_change_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(
        _deps(persona_brief_version="persona-v2")
    )


def test_plan_version_change_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(_deps(plan_version=4))


def test_rule_set_version_change_changes_hash() -> None:
    assert approval_dependency_hash(_deps()) != approval_dependency_hash(
        _deps(rule_set="CLAIM_PRICE:3,FORMAT_CONTROL:1")
    )


def test_rule_set_fingerprint_key() -> None:
    fingerprint = RuleSetFingerprint.from_rule_versions([("FORMAT_CONTROL", 1), ("CLAIM_PRICE", 2)])
    assert rule_set_version_key(fingerprint) == "CLAIM_PRICE:2,FORMAT_CONTROL:1"


def test_rule_set_key_none_is_empty() -> None:
    assert rule_set_version_key(None) == ""
    assert rule_set_version_key(tuple()) == ""


def test_identical_dependencies_give_identical_hash() -> None:
    assert approval_dependency_hash(_deps()) == approval_dependency_hash(_deps())


def test_deterministic_hash_length() -> None:
    digest = approval_dependency_hash(_deps())
    assert len(digest) == 64
    assert int(digest, 16) >= 0
