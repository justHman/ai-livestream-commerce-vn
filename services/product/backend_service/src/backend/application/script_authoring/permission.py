"""Authoring authorization — pure role checks (task 14.4).

Three capability levels map to existing admin/operator roles:

- ``can_author``: create/edit drafts and submit — any operator.
- ``can_spend``: AI-spend actions (generate/regenerate/fix) — operator
  WITH the explicit ``spend`` permission. Generation endpoints are never
  exposed anonymously or to viewers.
- ``can_approve``: human approval — admin or the dedicated ``approver``
  role. AI operations never produce APPROVED directly (task 2.3), and no
  generation endpoint is reachable without an authenticated actor.

Pure functions over a typed ``Actor``: no I/O, deterministic, unit-testable
without fixtures.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

__all__ = [
    "Actor",
    "can_author",
    "can_spend",
    "can_approve",
]

# Role identifiers, matching existing admin/operator conventions.
ROLE_OPERATOR = "operator"
ROLE_ADMIN = "admin"
ROLE_APPROVER = "approver"

# Capability permission identifier for AI-spend actions.
PERMISSION_SPEND = "spend"


@dataclass(frozen=True)
class Actor:
    """An authenticated actor: id + role set.

    ``permissions`` is an additional capability set (e.g. ``spend``) layered
    on top of roles so role membership stays coarse and spending stays
    explicit (design Decision 21: AI-spend is a distinct authorization).
    """

    id: str
    roles: frozenset[str] = frozenset()
    permissions: frozenset[str] = frozenset()


def can_author(actor: Optional[Actor]) -> bool:
    """Script authoring/edit/submit requires an operator (or stronger)."""
    return actor is not None and bool(actor.roles & {ROLE_OPERATOR, ROLE_ADMIN})


def can_spend(actor: Optional[Actor]) -> bool:
    """AI-spend actions require an operator WITH the explicit spend permission."""
    if actor is None:
        return False
    if not (actor.roles & {ROLE_OPERATOR, ROLE_ADMIN}):
        return False
    return PERMISSION_SPEND in actor.permissions


def can_approve(actor: Optional[Actor]) -> bool:
    """Human approval requires admin or the dedicated approver role."""
    return actor is not None and bool(actor.roles & {ROLE_ADMIN, ROLE_APPROVER})
