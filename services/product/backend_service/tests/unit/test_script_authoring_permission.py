"""Task 14.4 tests: authoring authorization separation.

Authoring/edit, AI-spend, and human approval are distinct capability
levels; generation endpoints are never exposed anonymously.
"""

from __future__ import annotations

from backend.application.script_authoring.permission import (
    PERMISSION_SPEND,
    ROLE_ADMIN,
    ROLE_APPROVER,
    ROLE_OPERATOR,
    Actor,
    can_approve,
    can_author,
    can_spend,
)

_OPERATOR = Actor(id="op-1", roles=frozenset({ROLE_OPERATOR}))
_OPERATOR_SPEND = Actor(
    id="op-2", roles=frozenset({ROLE_OPERATOR}), permissions=frozenset({PERMISSION_SPEND})
)
_ADMIN = Actor(id="admin-1", roles=frozenset({ROLE_ADMIN}))
_ADMIN_SPEND = Actor(
    id="admin-2", roles=frozenset({ROLE_ADMIN}), permissions=frozenset({PERMISSION_SPEND})
)
_APPROVER = Actor(id="ap-1", roles=frozenset({ROLE_APPROVER}))
_VIEWER = Actor(id="view-1", roles=frozenset())


def test_anonymous_is_not_permitted_anywhere():
    """No generation endpoint is reachable anonymously."""
    assert not can_author(None)
    assert not can_spend(None)
    assert not can_approve(None)


def test_operator_can_author_but_not_spend_without_permission():
    """Authoring is an operator capability; spending needs the spend permission."""
    assert can_author(_OPERATOR)
    assert not can_spend(_OPERATOR)


def test_operator_with_spend_permission_can_spend():
    assert can_spend(_OPERATOR_SPEND)


def test_admin_can_author_and_approve_but_spend_is_explicit():
    """Admin approves; AI-spend still requires the explicit permission."""
    assert can_author(_ADMIN)
    assert can_approve(_ADMIN)
    assert not can_spend(_ADMIN)


def test_admin_with_spend_permission_can_spend():
    assert can_spend(_ADMIN_SPEND)


def test_approver_can_approve_but_not_author_or_spend():
    """The approver role approves only — no authoring, no spending."""
    assert can_approve(_APPROVER)
    assert not can_author(_APPROVER)
    assert not can_spend(_APPROVER)


def test_viewer_with_no_roles_is_denied_everywhere():
    assert not can_author(_VIEWER)
    assert not can_spend(_VIEWER)
    assert not can_approve(_VIEWER)
