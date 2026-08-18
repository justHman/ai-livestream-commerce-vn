"""Task 12.1: session binding validation for approved ScriptSets.

Pure ``check_binding`` coverage: ScriptSet existence, required products,
approved versions, dependency freshness, transition/order compatibility,
and runtime catalog compatibility. Also covers the async
``validate_binding`` adapter against an in-memory BindingSource fake.
"""

from __future__ import annotations

import pytest

from backend.application.script_authoring.models import (
    Approval,
    LiveSessionBrief,
    ScriptItem,
    ScriptSet,
    ScriptState,
    ScriptVersion,
)
from backend.application.script_authoring.session_binding import (
    BindingCheck,
    BindingKind,
    DependencyFingerprint,
    RuntimePlan,
    RuntimeCatalogProxy,
    check_binding,
    validate_binding,
)

SET_ID = "script_set:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ITEM_ID_P1 = "script_item:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ITEM_ID_P2 = "script_item:cccccccccccccccccccccccccccccccc"
V1_ID = "script_version:dddddddddddddddddddddddddddddddd"
V2_ID = "script_version:eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
APPROVAL_ID = "approval:ffffffffffffffffffffffffffffffff"


class _Catalog:
    """Runtime catalog fake."""

    def __init__(self, product_ids: set[str]) -> None:
        self._ids = product_ids

    def contains(self, product_id: str) -> bool:
        return product_id in self._ids


def _approved_set(
    *,
    policy: str = "ORDER_AGNOSTIC",
    product_ids: list[str] | None = None,
    product_count: int = 2,
) -> ScriptSet:
    return ScriptSet(
        id=SET_ID,
        shop_id="shop-1",
        product_ids=product_ids or [f"P00{i + 1}" for i in range(product_count)],
        brief=LiveSessionBrief(transition_policy=policy),  # type: ignore[arg-type]
    )


def _item(
    product_id: str,
    item_id: str,
    *,
    state: ScriptState = ScriptState.APPROVED,
    approved_version_id: str = V1_ID,
) -> ScriptItem:
    return ScriptItem(
        id=item_id,
        script_set_id=SET_ID,
        product_id=product_id,
        state=state,
        approved_version_id=approved_version_id,
    )


def _version(state: ScriptState = ScriptState.APPROVED, version_id: str = V1_ID) -> ScriptVersion:
    return ScriptVersion(
        id=version_id,
        script_item_id=ITEM_ID_P1,
        version=1,
        state=state,
        spoken_text="Kem ABC chỉ 299.000 đồng.",
    )


def _approval(version_id: str = V1_ID) -> Approval:
    return Approval(
        id=APPROVAL_ID,
        script_item_id=ITEM_ID_P1,
        script_version_id=version_id,
        actor="admin",
        approval_hash="0" * 64,
        gate_run_id="gate_run:11111111111111111111111111111111",
    )


def _fresh_deps() -> DependencyFingerprint:
    return DependencyFingerprint(
        rule_set_version="rules-1",
        product_facts_version="facts-1",
        promotion_version="promo-1",
        persona_brief_version="persona-1",
    )


def _ready_binding(**overrides) -> dict:
    """A fully-ready binding payload with per-call overrides."""
    fresh = _fresh_deps()
    values = {
        "script_set": _approved_set(),
        "items_by_product": {
            "P001": _item("P001", ITEM_ID_P1),
            "P002": _item("P002", ITEM_ID_P2, approved_version_id=V2_ID),
        },
        "versions_by_item": {
            ITEM_ID_P1: _version(),
            ITEM_ID_P2: _version(version_id=V2_ID),
        },
        "approvals_by_item": {
            ITEM_ID_P1: _approval(),
            ITEM_ID_P2: _approval(version_id=V2_ID),
        },
        "current_dependencies": fresh,
        "recorded_dependencies_by_item": {
            ITEM_ID_P1: fresh,
            ITEM_ID_P2: fresh,
        },
        "runtime_plan": RuntimePlan(order_locked=False),
        "runtime_catalog": _Catalog({"P001", "P002"}),
    }
    values.update(overrides)
    return values


# --- happy path ----------------------------------------------------------


def test_ready_agnostic_set_binds_ok() -> None:
    check = check_binding(**_ready_binding())
    assert check.ok
    assert check.issues == ()


# --- ScriptSet existence --------------------------------------------------


def test_unknown_script_set_reported_unknown_set() -> None:
    check = check_binding(**_ready_binding(script_set=None))
    assert not check.ok
    assert len(check.issues) == 1
    issue = check.issues[0]
    assert issue.kind is BindingKind.UNKNOWN_SET
    assert issue.product_id == ""


# --- required products / missing items ------------------------------------


def test_missing_item_reported_missing() -> None:
    items = {
        "P001": _item("P001", ITEM_ID_P1),
        # P002 intentionally absent
    }
    check = check_binding(**_ready_binding(items_by_product=items))
    assert not check.ok
    assert [i.product_id for i in check.missing] == ["P002"]


def test_item_without_approved_version_reported_missing() -> None:
    items = {
        "P001": _item("P001", ITEM_ID_P1, approved_version_id=None),
        "P002": _item("P002", ITEM_ID_P2, approved_version_id=V2_ID),
    }
    check = check_binding(**_ready_binding(items_by_product=items))
    assert not check.ok
    assert [i.product_id for i in check.missing] == ["P001"]


def test_requested_products_scope_limits_checks() -> None:
    check = check_binding(**_ready_binding(requested_products=["P001"]))
    assert check.ok, "only P001 is required; P002 absent from items is fine"


# --- unapproved states ----------------------------------------------------


def test_unapproved_item_state_reported_unapproved() -> None:
    items = {
        "P001": _item("P001", ITEM_ID_P1, state=ScriptState.REVIEWABLE),
        "P002": _item("P002", ITEM_ID_P2, approved_version_id=V2_ID),
    }
    check = check_binding(**_ready_binding(items_by_product=items))
    assert not check.ok
    assert [i.product_id for i in check.unapproved] == ["P001"]


def test_persisted_version_state_does_not_block_binding() -> None:
    """Version.state is an immutable creation-time snapshot (Decision 13).

    A persisted non-APPROVED version does NOT block binding when the item is
    APPROVED, the version is the item's approved version, and the approval row
    binds that exact version. The version row's state never reflects approval.
    """
    versions = {
        ITEM_ID_P1: _version(state=ScriptState.REVIEWABLE),
        ITEM_ID_P2: _version(version_id=V2_ID),
    }
    check = check_binding(**_ready_binding(versions_by_item=versions))
    assert check.ok


def test_approval_missing_for_approved_version_reported_unapproved() -> None:
    approvals = {ITEM_ID_P2: _approval(version_id=V2_ID)}
    check = check_binding(**_ready_binding(approvals_by_item=approvals))
    assert not check.ok
    assert [i.product_id for i in check.unapproved] == ["P001"]


def test_approval_binding_other_version_reported_unapproved() -> None:
    approvals = {
        ITEM_ID_P1: _approval(version_id="script_version:99999999999999999999999999999999"),
        ITEM_ID_P2: _approval(version_id=V2_ID),
    }
    check = check_binding(**_ready_binding(approvals_by_item=approvals))
    assert not check.ok
    assert [i.product_id for i in check.unapproved] == ["P001"]


# --- dependency freshness --------------------------------------------------


def test_stale_dependencies_reported_stale() -> None:
    fresh = _fresh_deps()
    check = check_binding(
        **_ready_binding(
            recorded_dependencies_by_item={ITEM_ID_P1: fresh, ITEM_ID_P2: fresh},
            current_dependencies=DependencyFingerprint(
                rule_set_version="rules-2",  # changed after approval
                product_facts_version="facts-1",
                promotion_version="promo-1",
                persona_brief_version="persona-1",
            ),
        )
    )
    assert not check.ok
    assert [i.product_id for i in check.stale] == ["P001", "P002"]


def test_missing_recorded_dependencies_fail_closed_when_current_nonempty() -> None:
    """Binding is a readiness gate: unverifiable freshness fails closed.

    When the approval recorded no dependency fingerprint but the CURRENT
    dependencies carry versions, freshness cannot be confirmed — the gate
    reports STALE rather than risk speaking content bound to outdated facts.
    """
    check = check_binding(
        **_ready_binding(
            recorded_dependencies_by_item={},
            current_dependencies=DependencyFingerprint(
                rule_set_version="rules-2",
                product_facts_version="facts-1",
                promotion_version="promo-1",
                persona_brief_version="persona-1",
            ),
        )
    )
    assert not check.ok
    assert [i.product_id for i in check.stale] == ["P001", "P002"]


def test_empty_recorded_and_current_dependencies_not_stale() -> None:
    """Both sides empty (no versions persisted yet) => no staleness signal."""
    check = check_binding(
        **_ready_binding(
            recorded_dependencies_by_item={},
            current_dependencies=DependencyFingerprint(),
        )
    )
    assert check.ok


# --- transition / order compatibility --------------------------------------


def test_order_aware_set_requires_locked_runtime_plan() -> None:
    check = check_binding(
        **_ready_binding(
            script_set=_approved_set(policy="ORDER_AWARE"),
            runtime_plan=RuntimePlan(order_locked=False),
        )
    )
    assert not check.ok
    incompat = [i for i in check.incompatible if i.product_id == ""]
    assert len(incompat) == 1
    assert "ORDER_AWARE" in incompat[0].detail


def test_order_aware_set_with_locked_plan_is_compatible() -> None:
    check = check_binding(
        **_ready_binding(
            script_set=_approved_set(policy="ORDER_AWARE"),
            runtime_plan=RuntimePlan(order_locked=True),
        )
    )
    assert check.ok


# --- runtime catalog compatibility -----------------------------------------


def test_product_missing_from_runtime_catalog_incompatible() -> None:
    check = check_binding(
        **_ready_binding(runtime_catalog=_Catalog({"P001"}))  # P002 absent
    )
    assert not check.ok
    assert [i.product_id for i in check.incompatible] == ["P002"]


def test_empty_runtime_catalog_incompatible_for_all() -> None:
    check = check_binding(**_ready_binding(runtime_catalog=_Catalog(set())))
    assert not check.ok
    assert sorted(i.product_id for i in check.incompatible) == ["P001", "P002"]


# --- wire shape ------------------------------------------------------------


def test_check_as_dict_groups_stable_kinds() -> None:
    check = check_binding(
        **_ready_binding(
            script_set=_approved_set(policy="ORDER_AWARE", product_count=1),
            items_by_product={},
            runtime_plan=RuntimePlan(order_locked=False),
            runtime_catalog=_Catalog({"P001"}),
        )
    )
    payload = check.as_dict()
    assert payload["ok"] is False
    assert payload["missing"][0]["kind"] == "missing"
    assert payload["incompatible"][0]["kind"] == "incompatible"
    assert payload["issues"][0]["product_id"] == "P001"


# --- async validate_binding adapter ----------------------------------------


class _FakeBindingSource:
    """In-memory BindingSource storing domain models directly."""

    def __init__(self, script_set: ScriptSet | None, items, versions, approvals, deps) -> None:
        self._set = script_set
        self._items = items
        self._versions = versions
        self._approvals = approvals
        self._deps = deps

    async def get_script_set(self, *, set_id: str) -> ScriptSet | None:
        return self._set if self._set is not None and self._set.id == set_id else None

    async def get_script_item(self, *, set_id: str, product_id: str) -> ScriptItem | None:
        return self._items.get(product_id)

    async def get_script_version(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> ScriptVersion | None:
        item = self._items.get(product_id)
        if item is None:
            return None
        return self._versions.get(item.id)

    async def get_approval(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> Approval | None:
        item = self._items.get(product_id)
        if item is None:
            return None
        return self._approvals.get(item.id)

    def current_dependencies(self) -> DependencyFingerprint:
        return self._deps


@pytest.mark.asyncio
async def test_validate_binding_adapter_ok() -> None:
    source = _FakeBindingSource(
        _approved_set(),
        {
            "P001": _item("P001", ITEM_ID_P1),
            "P002": _item("P002", ITEM_ID_P2, approved_version_id=V2_ID),
        },
        {ITEM_ID_P1: _version(), ITEM_ID_P2: _version(version_id=V2_ID)},
        {ITEM_ID_P1: _approval(), ITEM_ID_P2: _approval(version_id=V2_ID)},
        _fresh_deps(),
    )
    check = await validate_binding(
        script_set_id=SET_ID,
        source=source,
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=_Catalog({"P001", "P002"}),
        recorded_dependencies_by_item={
            ITEM_ID_P1: _fresh_deps(),
            ITEM_ID_P2: _fresh_deps(),
        },
    )
    assert check.ok
    assert check.script_set is not None and check.script_set.id == SET_ID


@pytest.mark.asyncio
async def test_validate_binding_adapter_missing_set() -> None:
    source = _FakeBindingSource(None, {}, {}, {}, _fresh_deps())
    check = await validate_binding(
        script_set_id="script_set:99999999999999999999999999999999",
        source=source,
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=_Catalog(set()),
    )
    assert not check.ok
    assert check.issues[0].kind is BindingKind.UNKNOWN_SET


# ── C10 coverage additions ──────────────────────────────────────────────────


def test_ok_result_classmethod() -> None:
    s = _approved_set()
    check = BindingCheck.ok_result(s)
    assert check.ok
    assert check.script_set is s
    assert check.issues == ()


def test_requested_products_empty_scope_is_ok() -> None:
    check = check_binding(**_ready_binding(requested_products=[]))
    assert check.ok


# ── RuntimeCatalogProxy ─────────────────────────────────────────────────────


def test_catalog_proxy_none_director_returns_false() -> None:
    assert RuntimeCatalogProxy(director=None).contains("P001") is False


def test_catalog_proxy_finds_product_in_session_catalog() -> None:
    class _Doc:
        def __init__(self, id_: str) -> None:
            self.id = id_

    class _Session:
        def __init__(self, catalog) -> None:
            self.catalog = catalog

    class _Director:
        def __init__(self, sessions) -> None:
            self._sessions = sessions

    director = _Director({"s1": _Session([_Doc("P001"), _Doc("P002")])})
    proxy = RuntimeCatalogProxy(director=director)
    assert proxy.contains("P001") is True
    assert proxy.contains("P002") is True
    assert proxy.contains("P999") is False


def test_catalog_proxy_missing_sessions_attribute_returns_false() -> None:
    class _DirectorWithoutSessions:
        pass

    assert RuntimeCatalogProxy(director=_DirectorWithoutSessions()).contains("P001") is False


def test_catalog_proxy_empty_sessions_returns_false() -> None:
    class _Director:
        def __init__(self) -> None:
            self._sessions = {}

    assert RuntimeCatalogProxy(director=_Director()).contains("P001") is False


# ── validate_binding with plain-dict payloads (coerce branches) ─────────────


class _DictBindingSource:
    """BindingSource returning plain dicts (wire shape) instead of models."""

    def __init__(self, set_dict, items, versions, approvals, deps) -> None:
        self._set = set_dict
        self._items = items
        self._versions = versions
        self._approvals = approvals
        self._deps = deps

    async def get_script_set(self, *, set_id: str) -> dict | None:
        return self._set if self._set is not None and self._set.get("id") == set_id else None

    async def get_script_item(self, *, set_id: str, product_id: str) -> dict | None:
        return self._items.get(product_id)

    async def get_script_version(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> dict | None:
        item = self._items.get(product_id)
        return None if item is None else self._versions.get(item.get("id"))

    async def get_approval(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> dict | None:
        item = self._items.get(product_id)
        return None if item is None else self._approvals.get(item.get("id"))

    def current_dependencies(self) -> DependencyFingerprint:
        return self._deps


def _dict_payloads(brief) -> dict:
    set_dict = {
        "id": SET_ID,
        "shop_id": "shop-1",
        "title": "Set",
        "brief": brief,
        "product_ids": ["P001"],
        "revision": 0,
    }
    item_dict = {
        "id": ITEM_ID_P1,
        "script_set_id": SET_ID,
        "product_id": "P001",
        "state": "approved",
        "approved_version_id": V1_ID,
        "revision": 0,
    }
    version_dict = {
        "id": V1_ID,
        "script_item_id": ITEM_ID_P1,
        "version": 1,
        "state": "approved",
        "display_text": "d",
        "spoken_text": "s",
    }
    approval_dict = {
        "id": APPROVAL_ID,
        "script_item_id": ITEM_ID_P1,
        "script_version_id": V1_ID,
        "actor": "admin",
        "approval_hash": "0" * 64,
        "gate_run_id": "gate_run:11111111111111111111111111111111",
    }
    return set_dict, item_dict, version_dict, approval_dict


@pytest.mark.asyncio
async def test_validate_binding_dict_brief_as_dict() -> None:
    set_dict, item_dict, version_dict, approval_dict = _dict_payloads(
        {
            "title": "T",
            "persona": "p",
            "transition_policy": "ORDER_AGNOSTIC",
            "shop_name": "Shop A",
            "notes": "n",
        }
    )
    source = _DictBindingSource(
        set_dict,
        {"P001": item_dict},
        {ITEM_ID_P1: version_dict},
        {ITEM_ID_P1: approval_dict},
        _fresh_deps(),
    )
    check = await validate_binding(
        script_set_id=SET_ID,
        source=source,
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=_Catalog({"P001"}),
        recorded_dependencies_by_item={ITEM_ID_P1: _fresh_deps()},
    )
    assert check.ok


@pytest.mark.asyncio
async def test_validate_binding_dict_brief_as_model() -> None:
    set_dict, item_dict, version_dict, approval_dict = _dict_payloads(
        LiveSessionBrief(transition_policy="ORDER_AGNOSTIC")
    )
    source = _DictBindingSource(
        set_dict,
        {"P001": item_dict},
        {ITEM_ID_P1: version_dict},
        {ITEM_ID_P1: approval_dict},
        _fresh_deps(),
    )
    check = await validate_binding(
        script_set_id=SET_ID,
        source=source,
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=_Catalog({"P001"}),
        recorded_dependencies_by_item={ITEM_ID_P1: _fresh_deps()},
    )
    assert check.ok


@pytest.mark.asyncio
async def test_validate_binding_dict_without_brief() -> None:
    set_dict, item_dict, version_dict, approval_dict = _dict_payloads(None)
    source = _DictBindingSource(
        set_dict,
        {"P001": item_dict},
        {ITEM_ID_P1: version_dict},
        {ITEM_ID_P1: approval_dict},
        _fresh_deps(),
    )
    check = await validate_binding(
        script_set_id=SET_ID,
        source=source,
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=_Catalog({"P001"}),
        recorded_dependencies_by_item={ITEM_ID_P1: _fresh_deps()},
    )
    assert check.ok


@pytest.mark.asyncio
async def test_validate_binding_dict_item_missing_is_skipped() -> None:
    """A product whose item payload is None is skipped -> reported MISSING."""
    set_dict, item_dict, version_dict, approval_dict = _dict_payloads(None)
    source = _DictBindingSource(
        set_dict, {}, {ITEM_ID_P1: version_dict}, {ITEM_ID_P1: approval_dict}, _fresh_deps()
    )
    check = await validate_binding(
        script_set_id=SET_ID,
        source=source,
        runtime_plan=RuntimePlan(order_locked=False),
        runtime_catalog=_Catalog({"P001"}),
    )
    assert not check.ok
    assert check.issues[0].kind is BindingKind.MISSING
