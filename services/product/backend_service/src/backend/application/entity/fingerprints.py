"""Deterministic approval-freshness versions derived from entity facts (task 8.9).

Approval binding (Decision 14) needs a version value per authoritative
dependency: any fact revision/updated_at change MUST change the version so a
stale approval is detected. ``entity_facts_version`` digests the per-fact
``(key, revision, updated_at)`` triples of the entity's own facts — the
granular per-fact freshness the spec requires (never the document ``revision``
alone). ``promotion_version`` is the same digest restricted to the promotion
fact(s), so a promotion change stales only the promotion dependency.
"""

from __future__ import annotations

import hashlib

from .models import EntityDocument
from .registry import COMMERCE_PROMOTION

__all__ = ["entity_facts_version", "promotion_version"]


def entity_facts_version(entity: EntityDocument) -> str:
    """SHA256 over the entity's sorted ``(key, revision, updated_at)`` facts.

    Deterministic for identical input; any fact revision/updated_at change
    yields a different digest (the approval-freshness contract of task 8.9).
    """
    digest = hashlib.sha256()
    for key, revision, updated_at in sorted(
        (fact.key, fact.revision, fact.updated_at) for fact in entity.facts
    ):
        digest.update(f"{key}:{revision}:{updated_at}\n".encode("utf-8"))
    return digest.hexdigest()


def promotion_version(entity: EntityDocument) -> str:
    """SHA256 over the entity's promotion fact(s) — digest of the full set.

    Promotion facts are the volatile ``commerce.promotion`` entries; an empty
    set digests the empty string, so no-promotion stays stable across reads.
    """
    digest = hashlib.sha256()
    for key, revision, updated_at in sorted(
        (fact.key, fact.revision, fact.updated_at)
        for fact in entity.facts
        if fact.key == COMMERCE_PROMOTION
    ):
        digest.update(f"{key}:{revision}:{updated_at}\n".encode("utf-8"))
    return digest.hexdigest()
