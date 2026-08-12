"""Runtime ScriptSet binding validation (task 12.1).

Pure, deterministic readiness validation for binding a pre-live
``ScriptSet`` to a runtime session. Verifies, per required product:

- the ScriptSet exists and carries the requested products;
- each required product has an approved, current version;
- the approval's dependency fingerprint is fresh (no stale facts /
  promotions / persona / rule dependencies);
- the set's transition policy is compatible with the runtime plan
  (ORDER_AWARE sets only bind to a locked-order runtime plan, because
  their scripts may hard-code transitions; ORDER_AGNOSTIC sets bind to
  any plan and allow the Director to reorder products at runtime);
- every required product exists in the runtime catalog.

The sync core ``check_binding`` is pure (stdlib + ``script_authoring``
domain models); the async ``validate_binding`` adapter loads aggregate data
through a ``BindingSource`` (the ``ScriptAuthoringService`` protocol or an
in-memory fake) and runs the same core. Nothing here mutates authoring
artifacts: binding writes a snapshot into session/runtime state (task
12.3), never into ScriptSet/ScriptItem/Approval rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Optional, Protocol, Sequence

from backend.application.script_authoring.gate.context import TransitionPolicy
from backend.application.script_authoring.models import (
    Approval,
    LiveSessionBrief,
    ScriptItem,
    ScriptSet,
    ScriptState,
    ScriptVersion,
)

__all__ = [
    "BindingCheck",
    "BindingIssue",
    "BindingKind",
    "BindingSource",
    "DependencyFingerprint",
    "RuntimeCatalog",
    "RuntimeCatalogProxy",
    "RuntimePlan",
    "check_binding",
    "validate_binding",
]


class BindingKind(StrEnum):
    """Stable machine-readable binding issue categories."""

    MISSING = "missing"  # product has no script item / no approved version
    UNAPPROVED = "unapproved"  # latest current version is not APPROVED
    STALE = "stale"  # approval dependency fingerprint no longer fresh
    INCOMPATIBLE = "incompatible"  # transition policy / catalog mismatch
    UNKNOWN_SET = "unknown_set"  # script set does not exist


@dataclass(frozen=True)
class BindingIssue:
    """One binding failure with stable machine-readable details."""

    product_id: str
    kind: BindingKind
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        """Stable wire shape: ``{"product_id", "kind", "detail"}``."""
        return {"product_id": self.product_id, "kind": self.kind.value, "detail": self.detail}


@dataclass(frozen=True)
class DependencyFingerprint:
    """Authoritative dependency versions an approval is bound to.

    Mirrors the Decision 14 approval-hash inputs (rule set, product facts,
    promotion, persona brief). The binding validator compares the versions
    RECORDED at approval time against the CURRENT versions to detect
    staleness without parsing the opaque approval hash. The approval record
    itself is a typed model that forbids extra fields, so the recorded
    fingerprint travels alongside it (``recorded_dependencies_by_item``)
    rather than on the approval row — the authoring service persists both
    together at approval time (task 4.4).
    """

    rule_set_version: str = ""
    product_facts_version: str = ""
    promotion_version: str = ""
    persona_brief_version: str = ""


@dataclass(frozen=True)
class RuntimePlan:
    """The runtime run plan's order policy (Decision 15).

    ``order_locked`` mirrors whether the runtime plan pins product order:
    True for ORDER_AWARE-style locked runs, False when the Director may
    reorder products (ORDER_AGNOSTIC runtime selection).
    """

    order_locked: bool = False


class RuntimeCatalog(Protocol):
    """Minimal runtime catalog surface the binding validator needs."""

    def contains(self, product_id: str) -> bool: ...


class RuntimeCatalogProxy:
    """Runtime catalog adapter backed by the Director runtime.

    The runtime catalog is the list of products attached to the session via
    ``/sessions/{id}/attach``; it lives on the container's director runtime.
    An absent runtime is treated as an empty catalog so binding rejects
    products that cannot be spoken.
    """

    def __init__(self, director: Any = None, coordinator: Any = None) -> None:
        self._director = director
        self._coordinator = coordinator

    def contains(self, product_id: str) -> bool:
        runtime = self._director
        if runtime is None:
            return False
        try:
            # DirectorRuntime catalog: per-session product id set.
            for session in runtime._sessions.values():
                if any(p.product_id == product_id for p in session.catalog):
                    return True
            return False
        except AttributeError:
            return False


@dataclass(frozen=True)
class BindingCheck:
    """Result of ``check_binding``.

    ``ok`` is True iff there are zero issues. ``missing``/``stale``/
    ``incompatible`` group issues by ``BindingKind`` so the API can render
    the structured 409 payload from Decision 16 without re-parsing.
    """

    ok: bool
    script_set: Optional[ScriptSet] = None
    issues: tuple[BindingIssue, ...] = ()
    missing: tuple[BindingIssue, ...] = field(default_factory=tuple)
    stale: tuple[BindingIssue, ...] = field(default_factory=tuple)
    incompatible: tuple[BindingIssue, ...] = field(default_factory=tuple)
    unapproved: tuple[BindingIssue, ...] = field(default_factory=tuple)

    @classmethod
    def ok_result(cls, script_set: ScriptSet) -> "BindingCheck":
        return cls(ok=True, script_set=script_set)

    @classmethod
    def failed(cls, issues: Sequence[BindingIssue]) -> "BindingCheck":
        return cls(
            ok=False,
            issues=tuple(issues),
            missing=tuple(i for i in issues if i.kind is BindingKind.MISSING),
            stale=tuple(i for i in issues if i.kind is BindingKind.STALE),
            incompatible=tuple(i for i in issues if i.kind is BindingKind.INCOMPATIBLE),
            unapproved=tuple(i for i in issues if i.kind is BindingKind.UNAPPROVED),
        )

    def as_dict(self) -> dict[str, object]:
        """Stable wire shape for the 409 response body.

        ``issues`` is a flat structured list; the grouped fields are
        convenience views for clients that only need one category.
        """
        return {
            "ok": self.ok,
            "issues": [issue.as_dict() for issue in self.issues],
            "missing": [issue.as_dict() for issue in self.missing],
            "stale": [issue.as_dict() for issue in self.stale],
            "incompatible": [issue.as_dict() for issue in self.incompatible],
            "unapproved": [issue.as_dict() for issue in self.unapproved],
        }


def _issue(product_id: str, kind: BindingKind, detail: str = "") -> BindingIssue:
    return BindingIssue(product_id=product_id, kind=kind, detail=detail)


def _transition_incompatibility(
    script_set: ScriptSet, runtime_plan: RuntimePlan
) -> Optional[BindingIssue]:
    """ORDER_AWARE sets need a locked-order runtime plan (Decision 15).

    An ORDER_AWARE script may hard-code transitions to adjacent products;
    an unlocked runtime plan lets the Director reorder products, which would
    make those baked transitions wrong. ORDER_AGNOSTIC sets are always
    compatible with any runtime plan.
    """
    policy: TransitionPolicy = script_set.brief.transition_policy
    if policy == "ORDER_AWARE" and not runtime_plan.order_locked:
        return _issue(
            "",
            BindingKind.INCOMPATIBLE,
            "ORDER_AWARE script set requires a locked-order runtime plan",
        )
    return None


def check_binding(
    *,
    script_set: Optional[ScriptSet],
    items_by_product: dict[str, ScriptItem],
    versions_by_item: dict[str, ScriptVersion],
    approvals_by_item: dict[str, Approval],
    current_dependencies: DependencyFingerprint,
    runtime_plan: RuntimePlan,
    runtime_catalog: RuntimeCatalog,
    requested_products: Optional[Sequence[str]] = None,
    recorded_dependencies_by_item: Optional[dict[str, DependencyFingerprint]] = None,
) -> BindingCheck:
    """Pure sync validation core (task 12.1). See module docstring.

    Checks, per required product (``requested_products`` or the set's own
    ordered products):
      1. existence of a ScriptItem with an approved version (MISSING);
      2. the current version is APPROVED (UNAPPROVED);
      3. the approval's dependency fingerprint is fresh vs
         ``current_dependencies`` (STALE) — the versions recorded at
         approval time come from ``recorded_dependencies_by_item``
         (default: all-"" fingerprint, i.e. no staleness signal);
      4. the set's transition policy is compatible with the runtime plan
         (INCOMPATIBLE): ORDER_AWARE requires a locked-order runtime plan;
      5. the product exists in the runtime catalog (INCOMPATIBLE).

    The ScriptSet itself missing is reported as a single UNKNOWN_SET issue
    with product_id "".
    """
    if script_set is None:
        return BindingCheck.failed([_issue("", BindingKind.UNKNOWN_SET, "script set not found")])

    recorded_by_item = recorded_dependencies_by_item or {}

    required = (
        list(requested_products) if requested_products is not None else list(script_set.product_ids)
    )

    issues: list[BindingIssue] = []
    for product_id in required:
        item = items_by_product.get(product_id)
        if item is None:
            issues.append(_issue(product_id, BindingKind.MISSING, "no script item for product"))
            continue
        version = versions_by_item.get(item.id)
        if version is None or version.id != item.approved_version_id:
            issues.append(
                _issue(product_id, BindingKind.MISSING, "no approved version for product")
            )
            continue
        if item.state is not ScriptState.APPROVED or version.state is not ScriptState.APPROVED:
            issues.append(_issue(product_id, BindingKind.UNAPPROVED, "script is not APPROVED"))
            continue
        approval = approvals_by_item.get(item.id)
        if approval is None or approval.script_version_id != version.id:
            issues.append(
                _issue(
                    product_id,
                    BindingKind.UNAPPROVED,
                    "approval does not bind the current version",
                )
            )
            continue
        recorded = recorded_by_item.get(item.id, DependencyFingerprint())
        if recorded != current_dependencies:
            issues.append(
                _issue(
                    product_id,
                    BindingKind.STALE,
                    "approval dependencies changed since approval",
                )
            )
        if not runtime_catalog.contains(product_id):
            issues.append(
                _issue(
                    product_id,
                    BindingKind.INCOMPATIBLE,
                    "product missing from runtime catalog",
                )
            )

    transition_issue = _transition_incompatibility(script_set, runtime_plan)
    if transition_issue is not None:
        issues.append(transition_issue)

    if not issues:
        return BindingCheck.ok_result(script_set)
    return BindingCheck.failed(issues)


class BindingSource(Protocol):
    """Aggregate data source for the async ``validate_binding`` adapter.

    The container-scoped ``ScriptAuthoringService`` satisfies this protocol
    structurally; tests inject an in-memory fake.
    """

    async def get_script_set(self, *, set_id: str) -> dict[str, Any] | None: ...

    async def get_script_item(self, *, set_id: str, product_id: str) -> dict[str, Any] | None: ...

    async def get_script_version(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> dict[str, Any] | None: ...

    async def get_approval(
        self, *, set_id: str, product_id: str, version_id: str | None
    ) -> dict[str, Any] | None: ...

    def current_dependencies(self) -> DependencyFingerprint: ...


async def validate_binding(
    *,
    script_set_id: str,
    source: BindingSource,
    runtime_plan: RuntimePlan,
    runtime_catalog: RuntimeCatalog,
    requested_products: Optional[Sequence[str]] = None,
    recorded_dependencies_by_item: Optional[dict[str, DependencyFingerprint]] = None,
) -> BindingCheck:
    """Async adapter: load aggregate data via ``source``, run ``check_binding``.

    The ScriptSet payload is a plain dict in the wire shape (``id``,
    ``product_ids``, ``brief``) unless the source returns a typed
    ``ScriptSet`` (the in-memory fake does). Items/versions/approvals are
    resolved per product and mapped by their stable ids. The recorded
    dependency fingerprint per item (persisted at approval time) may be
    supplied explicitly; when omitted, no staleness signal is raised.
    """
    raw_set = await source.get_script_set(set_id=script_set_id)
    script_set = _coerce_script_set(raw_set)
    if script_set is None:
        return check_binding(
            script_set=None,
            items_by_product={},
            versions_by_item={},
            approvals_by_item={},
            current_dependencies=source.current_dependencies(),
            runtime_plan=runtime_plan,
            runtime_catalog=runtime_catalog,
            requested_products=requested_products,
        )

    items_by_product: dict[str, ScriptItem] = {}
    versions_by_item: dict[str, ScriptVersion] = {}
    approvals_by_item: dict[str, Approval] = {}
    for product_id in script_set.product_ids:
        raw_item = await source.get_script_item(set_id=script_set_id, product_id=product_id)
        item = _coerce_script_item(raw_item)
        if item is None:
            continue
        items_by_product[product_id] = item
        raw_version = await source.get_script_version(
            set_id=script_set_id, product_id=product_id, version_id=item.approved_version_id
        )
        version = _coerce_script_version(raw_version)
        if version is not None:
            versions_by_item[item.id] = version
        raw_approval = await source.get_approval(
            set_id=script_set_id, product_id=product_id, version_id=item.approved_version_id
        )
        approval = _coerce_approval(raw_approval)
        if approval is not None:
            approvals_by_item[item.id] = approval

    return check_binding(
        script_set=script_set,
        items_by_product=items_by_product,
        versions_by_item=versions_by_item,
        approvals_by_item=approvals_by_item,
        current_dependencies=source.current_dependencies(),
        runtime_plan=runtime_plan,
        runtime_catalog=runtime_catalog,
        requested_products=requested_products,
        recorded_dependencies_by_item=recorded_dependencies_by_item,
    )


def _coerce_script_set(raw: Any) -> Optional[ScriptSet]:
    if raw is None:
        return None
    if isinstance(raw, ScriptSet):
        return raw
    if isinstance(raw, dict):
        brief = raw.get("brief")
        if isinstance(brief, LiveSessionBrief):
            brief_model = brief
        elif isinstance(brief, dict):
            brief_model = LiveSessionBrief(**brief)
        else:
            brief_model = LiveSessionBrief()
        return ScriptSet(
            id=raw.get("id", ""),
            shop_id=raw.get("shop_id", ""),
            title=raw.get("title", ""),
            brief=brief_model,
            product_ids=list(raw.get("product_ids") or []),
            revision=raw.get("revision", 0),
        )
    return None


def _coerce_script_item(raw: Any) -> Optional[ScriptItem]:
    if raw is None:
        return None
    if isinstance(raw, ScriptItem):
        return raw
    if isinstance(raw, dict):
        return ScriptItem(
            id=raw.get("id", ""),
            script_set_id=raw.get("script_set_id", ""),
            product_id=raw.get("product_id", ""),
            state=ScriptState(raw.get("state", ScriptState.EMPTY.value)),
            approved_version_id=raw.get("approved_version_id"),
            revision=raw.get("revision", 0),
        )
    return None


def _coerce_script_version(raw: Any) -> Optional[ScriptVersion]:
    if raw is None:
        return None
    if isinstance(raw, ScriptVersion):
        return raw
    if isinstance(raw, dict):
        return ScriptVersion(
            id=raw.get("id", ""),
            script_item_id=raw.get("script_item_id", ""),
            version=raw.get("version", 1),
            state=ScriptState(raw.get("state", ScriptState.DRAFT.value)),
            display_text=raw.get("display_text", ""),
            spoken_text=raw.get("spoken_text", ""),
        )
    return None


def _coerce_approval(raw: Any) -> Optional[Approval]:
    if raw is None:
        return None
    if isinstance(raw, Approval):
        return raw
    if isinstance(raw, dict):
        return Approval(
            id=raw.get("id", ""),
            script_item_id=raw.get("script_item_id", ""),
            script_version_id=raw.get("script_version_id", ""),
            actor=raw.get("actor", ""),
            approval_hash=raw.get("approval_hash", ""),
            gate_run_id=raw.get("gate_run_id", ""),
        )
    return None
