"""E2E session binding over real PG (Change B, B8).

Approves a script exactly as the manual path does, then validates that:

- a ``BindingSource`` adapter over the SAME repos returns DOMAIN MODELS that
  satisfy ``check_binding`` (happy path);
- ``resolve_approved_script`` resolves the EXACT saved ``spoken_text``;
- a changed current dependency fingerprint is reported STALE.

The persisted ``ScriptVersion.state`` is an immutable creation-time snapshot
(Decision 13) and never flips to APPROVED, so a REAL approved script must bind
on ``(item.state == APPROVED)`` + ``(approval binds current version)``, not on
the version row's state.
"""

from __future__ import annotations

import pytest

from backend.application.db.postgres_store import PostgresRuntimeStore
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.repositories import PostgresAuthoringRepositories
from backend.application.script_authoring.runtime_handoff import (
    ResolvedApprovedScript,
    resolve_approved_script,
)
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.application.script_authoring.session_binding import (
    BindingKind,
    DependencyFingerprint,
    RuntimePlan,
    check_binding,
    validate_binding,
)
from backend.config import ScriptAuthoringConfig


async def _connect(pg_url: str) -> PostgresAuthoringRepositories:
    store = PostgresRuntimeStore(pg_url)
    await store.connect()
    await store.apply_schema()
    repos = PostgresAuthoringRepositories(pg_url)
    await repos.connect()
    return repos


def _long_spoken() -> str:
    """A script whose estimated spoken duration lands inside [300, 3600]s."""
    sentence = "Kem dưỡng da này giúp làn da mịn màng và tươi sáng mỗi ngày."
    return " ".join([sentence] * 200)


async def _approved_script(pg_url: str):
    """Create + draft + submit + approve one product via the real service.

    Returns the live repos (caller owns ``close()``) plus the approved
    aggregate pieces and the exact approved ``spoken_text``.
    """
    repos = await _connect(pg_url)
    service = ScriptAuthoringServiceImpl(repos, config=ScriptAuthoringConfig())
    created = await service.create_script_set(
        name="Set Bind", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=None
    )
    set_id = created["id"]
    spoken = _long_spoken()
    await service.save_draft(
        set_id=set_id, product_id="P1", display_text=spoken, spoken_text=spoken, revision=None
    )
    submitted = await service.submit_for_gate(set_id=set_id, product_id="P1")
    assert submitted["state"] == "REVIEWABLE"
    item = await repos.items.get_by_product(set_id, "P1")
    assert item is not None and item.current_version_id is not None
    await service.approve_product(
        set_id=set_id, product_id="P1", version_id=item.current_version_id, actor="admin"
    )
    item = await repos.items.get_by_product(set_id, "P1")
    assert item is not None and item.state is ScriptState.APPROVED
    version = await repos.versions.get(item.approved_version_id)
    approval = await repos.approvals.get_by_item(item.id)
    recorded = await repos.approvals.recorded_dependencies(item.id)
    return repos, set_id, item, version, approval, recorded, spoken


class _RepoBindingSource:
    """BindingSource over real PG repos returning DOMAIN MODELS."""

    def __init__(self, repos, current_dependencies: DependencyFingerprint) -> None:
        self._repos = repos
        self._current_dependencies = current_dependencies

    async def get_script_set(self, *, set_id: str):
        return await self._repos.script_sets.get(set_id)

    async def get_script_item(self, *, set_id: str, product_id: str):
        return await self._repos.items.get_by_product(set_id, product_id)

    async def get_script_version(self, *, set_id: str, product_id: str, version_id: str | None):
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            return None
        vid = version_id if version_id is not None else item.approved_version_id
        if vid is None:
            return None
        return await self._repos.versions.get(vid)

    async def get_approval(self, *, set_id: str, product_id: str, version_id: str | None):
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            return None
        return await self._repos.approvals.get_by_item(item.id)

    def current_dependencies(self) -> DependencyFingerprint:
        return self._current_dependencies


class _RepoApprovedScriptStore:
    """ApprovedScriptStore over real PG repos.

    The repo reads are async; ``resolve_approved_script`` awaits an awaitable
    return value transparently.
    """

    def __init__(self, repos) -> None:
        self._repos = repos

    async def get_approved_version(self, *, script_set_id: str, product_id: str):
        item = await self._repos.items.get_by_product(script_set_id, product_id)
        if item is None or item.approved_version_id is None:
            return None
        version = await self._repos.versions.get_approved(item.id)
        if version is None:
            return None
        return ResolvedApprovedScript(
            product_id=item.product_id,
            approved_version_id=item.approved_version_id,
            spoken_text=version.spoken_text,
        )


class _Catalog:
    """Runtime catalog fake: contains() True for the given product ids."""

    def __init__(self, product_ids: list[str]) -> None:
        self._ids = set(product_ids)

    def contains(self, product_id: str) -> bool:
        return product_id in self._ids


def _recorded_fingerprint(recorded: dict) -> DependencyFingerprint:
    return DependencyFingerprint(
        rule_set_version=recorded.get("rule_set", ""),
        product_facts_version=recorded.get("product_facts_version", ""),
        promotion_version=recorded.get("promotion_version", ""),
        persona_brief_version=recorded.get("persona_brief_version", ""),
    )


def _bind_payload(
    item,
    version,
    approval,
    recorded_fp: DependencyFingerprint,
    current_fp: DependencyFingerprint,
) -> dict:
    return dict(
        items_by_product={item.product_id: item},
        versions_by_item={item.id: version},
        approvals_by_item={item.id: approval},
        current_dependencies=current_fp,
        recorded_dependencies_by_item={item.id: recorded_fp},
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=_Catalog([item.product_id]),
        requested_products=[item.product_id],
    )


@pytest.mark.asyncio
async def test_session_binding_happy_path_binds_exact_spoken_text_pg(pg_url: str) -> None:
    repos, set_id, item, version, approval, recorded, spoken = await _approved_script(pg_url)
    try:
        fingerprint = _recorded_fingerprint(recorded)

        # Direct sync core over the exact approved aggregate (the real caller).
        check = check_binding(
            script_set=await repos.script_sets.get(set_id),
            **_bind_payload(item, version, approval, fingerprint, fingerprint),
        )
        assert check.ok, check.issues

        # Async adapter over the SAME repos also binds OK.
        source = _RepoBindingSource(repos, fingerprint)
        async_check = await validate_binding(
            script_set_id=set_id,
            source=source,
            runtime_plan=RuntimePlan(order_locked=False),
            runtime_catalog=_Catalog([item.product_id]),
            requested_products=[item.product_id],
            recorded_dependencies_by_item={item.id: fingerprint},
        )
        assert async_check.ok, async_check.issues

        # The approved store resolves the EXACT approved spoken_text.
        store = _RepoApprovedScriptStore(repos)
        resolved = await resolve_approved_script(store, script_set_id=set_id, product_id="P1")
        assert resolved is not None
        assert resolved.approved_version_id == item.approved_version_id
        assert resolved.spoken_text == spoken
    finally:
        await repos.close()


@pytest.mark.asyncio
async def test_session_binding_stale_dependencies_reported_pg(pg_url: str) -> None:
    repos, set_id, item, version, approval, recorded, _spoken = await _approved_script(pg_url)
    try:
        recorded_fp = _recorded_fingerprint(recorded)
        current_fp = DependencyFingerprint(
            rule_set_version="rules-2",
            product_facts_version=recorded_fp.product_facts_version,
            promotion_version=recorded_fp.promotion_version,
            persona_brief_version=recorded_fp.persona_brief_version,
        )
        check = check_binding(
            script_set=await repos.script_sets.get(set_id),
            **_bind_payload(item, version, approval, recorded_fp, current_fp),
        )
        assert not check.ok
        assert [i.kind for i in check.stale] == [BindingKind.STALE]
        assert [i.product_id for i in check.stale] == [item.product_id]
    finally:
        await repos.close()
