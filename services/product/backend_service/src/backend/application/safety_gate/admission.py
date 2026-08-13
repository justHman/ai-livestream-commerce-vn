"""Admission contract: the single gate point between the Safety Gate and
downstream pipelines (embedding, clustering, demand scoring, Agent context).

OpenSpec cluster C3 (design Decision 3): the Safety Gate runs BEFORE any
embedding or Agent context creation. Every rejected decision MUST NOT proceed
to embedder calls, semantic cluster membership, demand counts, or Agent
context. Accepted-with-signal decisions MAY proceed — signals are advisory
(they carry no reason code) and are only recorded in content-safe metrics.

The ingestion layer (C2) owns the full drain -> gate -> embed -> route ->
state loop; this module only documents the boundary and exposes one pure
predicate so the caller's gate-consultation point is a single, testable name.
"""

from __future__ import annotations

from .decision import SafetyDecision

__all__ = ["should_embed"]


def should_embed(decision: SafetyDecision) -> bool:
    """Whether a gated comment may proceed to embed/cluster/demand/Agent.

    True only for accepted decisions. A rejected decision (any reason code)
    must be dropped before embedding; accepted-with-signal decisions are
    admitted because rejection signals are advisory.
    """
    return decision.accepted
