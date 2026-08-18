"""Concrete zero-LLM ``ScriptAuthoringService`` over the SQL repositories (Change B, B4).

Implements the manual authoring path — create/get/update ScriptSet,
``save_draft``, ``submit_for_gate``, ``preview_product``,
``approve_product``/``approve_batch`` — against ``PostgresAuthoringRepositories``
using the deterministic ``ProductGenerationWorkflow`` FSM and the real
``ScriptGate``. AI generation/fix/regenerate/batch methods are stubs that raise
``ScriptAuthoringError("llm_unavailable", ...)`` until B5/B6 wires them.

Design decisions:
  - SyncPersistBridge: the FSM's synchronous ``persist`` hook pushes the
    mutated ``ScriptItem`` onto a thread-safe queue; after each command the
    service drains the queue and writes the item (plus any new version / gate
    run derived from ``workflow.current_version`` / ``workflow.last_gate_run``)
    in ONE repository transaction, guarded by the item revision read BEFORE the
    workflow ran.
  - Version rows are immutable: ``ScriptVersion.state`` is never UPDATE'd; the
    item's ``state`` is the wire source of truth (Decisions 13/14).
  - Approval re-runs the Full Script Gate exactly once for the exact current
    version and binds the approval hash to the dependency versions (Decision 14).
"""

from __future__ import annotations

import queue
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from backend.application.script_authoring.approval import (
    ApprovalError,
    ApprovalRequest,
    approve_script,
)
from backend.application.script_authoring.compile import compile_spoken_text
from backend.application.script_authoring.fingerprints import (
    ApprovalDependencies,
    rule_set_version_key,
)
from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext
from backend.application.script_authoring.gate.engine import (
    ScriptGate,
    default_full_script_rules,
    default_segment_rules,
)
from backend.application.script_authoring.gate.registry import ScriptRuleRegistry
from backend.application.script_authoring.gate.results import GateRunResult, RuleViolation
from backend.application.script_authoring.generation.calibration import (
    GenerationBudgetCalibration,
    GenerationBudgetError,
)
from backend.application.script_authoring.generation.preview import (
    preview_product as compute_product_preview,
)
from backend.application.script_authoring.models import (
    Approval,
    GateRun,
    GateViolation,
    LiveSessionBrief,
    ScriptItem,
    ScriptSet,
    ScriptState,
    ScriptVersion,
    new_id,
)
from backend.application.script_authoring.repositories import (
    PostgresAuthoringRepositories,
    StaleRevisionError,
)
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.state import IllegalTransitionError
from backend.application.script_authoring.workflow import ProductGenerationWorkflow
from backend.config import ScriptAuthoringConfig

__all__ = ["ScriptAuthoringServiceImpl"]


class _SyncPersistBridge:
    """Thread-safe sink for the FSM's synchronous ``persist`` hook.

    The FSM calls ``persist(item)`` after every persisted change; the service
    drains the queue once per command and writes the collected items inside a
    single repository transaction.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[ScriptItem] = queue.Queue()

    def __call__(self, item: ScriptItem) -> None:
        self._queue.put(item)

    def drain(self) -> list[ScriptItem]:
        items: list[ScriptItem] = []
        while True:
            try:
                items.append(self._queue.get_nowait())
            except queue.Empty:
                return items


@dataclass(frozen=True)
class _GateResultWithId:
    """Frozen ``GateRunResult``-shaped proxy carrying the pre-created gate_run_id.

    ``approval.approve_script`` reads ``scope``, ``passed``, and optionally
    ``gate_run_id``; the underlying ``GateRunResult`` is frozen, so this proxy
    surfaces the id without mutating the result.
    """

    result: GateRunResult
    gate_run_id: str

    @property
    def scope(self) -> str:
        return self.result.scope

    @property
    def passed(self) -> bool:
        return self.result.passed


class _ApprovalGateChecker:
    """``GateChecker`` returning a precomputed result tagged with the run id.

    The checker never re-runs the gate; ``approve_product`` runs the gate
    exactly once and reuses that outcome for the approval record.
    """

    def __init__(self, run_id: str, result: GateRunResult) -> None:
        self._run_id = run_id
        self._result = result

    def check_full_script(self, _compiled_spoken_text: str) -> GateRunResult:
        return _GateResultWithId(self._result, self._run_id)


def _to_gate_violation(violation: RuleViolation) -> GateViolation:
    """Map a gate ``RuleViolation`` to the persisted ``GateViolation`` model."""
    return GateViolation(
        rule_id=violation.rule_id,
        severity=violation.severity.value,
        message=violation.message,
        segment_index=violation.segment_index,
        span_start=violation.text_span.start if violation.text_span else None,
        span_end=violation.text_span.end if violation.text_span else None,
    )


def _approval_dependencies_dict(deps: ApprovalDependencies) -> dict[str, Any]:
    """Recorded-dependencies payload shape for ``ApprovalRepository.insert``."""
    return {
        "spoken_text": deps.spoken_text,
        "segment_hashes": list(deps.segment_hashes),
        "plan_version": deps.plan_version,
        "rule_set": deps.rule_set,
        "product_facts_version": deps.product_facts_version,
        "promotion_version": deps.promotion_version,
        "persona_brief_version": deps.persona_brief_version,
    }


class ScriptAuthoringServiceImpl:
    """Zero-LLM concrete ``ScriptAuthoringService`` (Change B, B4).

    All commands are async; AI generation commands raise ``llm_unavailable``
    until B5/B6 wires the generator/regenerator/fixer/batch scheduler.
    """

    def __init__(
        self,
        repos: PostgresAuthoringRepositories,
        *,
        config: ScriptAuthoringConfig | None = None,
        gate: ScriptGate | None = None,
    ) -> None:
        self._repos = repos
        self._config = config or ScriptAuthoringConfig()
        self._gate = gate or self._default_gate()

    # ── construction / helpers ───────────────────────────────────────

    @staticmethod
    def _default_gate() -> ScriptGate:
        segment_rules = default_segment_rules()
        full_rules = default_full_script_rules()
        registry = ScriptRuleRegistry([*segment_rules.rules, *full_rules.rules])
        return ScriptGate(registry, segment_rules, full_rules)

    @staticmethod
    def _raise_not_found(kind: str, ident: str) -> NoReturn:
        raise ScriptAuthoringError("not_found", f"{kind} {ident} not found")

    @staticmethod
    def _brief_from_dict(brief: dict[str, Any] | None, transition_policy: str) -> LiveSessionBrief:
        brief = brief or {}
        return LiveSessionBrief(
            title=brief.get("title", ""),
            persona=brief.get("persona", brief.get("host_name", "")),
            shop_name=brief.get("shop_name", ""),
            notes=brief.get("notes", brief.get("note", "")),
            transition_policy=transition_policy,
        )

    @staticmethod
    def _set_wire(script_set: ScriptSet, items: list[ScriptItem]) -> dict[str, Any]:
        return {
            "id": script_set.id,
            "name": script_set.title,
            "transition_policy": script_set.brief.transition_policy,
            "product_ids": list(script_set.product_ids),
            "revision": script_set.revision,
            "items": {item.product_id: {"state": item.state.name} for item in items},
        }

    @staticmethod
    def _gate_wire(result: GateRunResult) -> dict[str, Any]:
        return {
            "state": "passed" if result.passed else "gate_failed",
            "violations": [
                {
                    "rule_id": violation.rule_id,
                    "severity": violation.severity.value,
                    "message": violation.message,
                }
                for violation in result.violations
            ],
        }

    def _calibration(self) -> GenerationBudgetCalibration:
        return GenerationBudgetCalibration(
            model_max_output_tokens=self._config.budget_max_output_tokens,
            output_safety_factor=self._config.budget_output_safety_factor,
            min_target_duration_s=self._config.min_target_duration_s,
            max_target_duration_s=self._config.max_target_duration_s,
        )

    def _make_workflow(
        self,
        item: ScriptItem,
        current_version: ScriptVersion | None,
        persist: _SyncPersistBridge,
        transition_policy: str,
    ) -> ProductGenerationWorkflow:
        gate = self._gate
        context = ScriptGateContext(transition_policy=transition_policy, facts=ProductFacts())

        def segment_gate(text: str) -> GateRunResult:
            return gate.run_segment(text, context)

        def full_gate(segments: Sequence[str]) -> GateRunResult:
            return gate.run_full_script(list(segments), context)

        workflow = ProductGenerationWorkflow(
            item=item,
            segment_gate=segment_gate,
            full_gate=full_gate,
            persist=persist,
        )
        if current_version is not None:
            workflow.versions = [current_version]
            workflow.current_version = current_version
        return workflow

    async def _persist_workflow(
        self,
        workflow: ProductGenerationWorkflow,
        bridge: _SyncPersistBridge,
        *,
        item_revision: int,
        existing_version_ids: set[str],
    ) -> None:
        """Write workflow-persisted items + new version / gate run in one tx."""
        # The FSM persists the same item object after each transition; dedupe
        # by id so the optimistic-lock UPDATE runs exactly once.
        persisted: dict[str, ScriptItem] = {}
        for item in bridge.drain():
            persisted[item.id] = item
        try:
            async with self._repos.transaction() as conn:
                # Insert immutable version + gate run rows BEFORE the item
                # update: script_items.current_version_id has a FK to
                # script_versions.id, so the version must exist first.
                if (
                    workflow.current_version is not None
                    and workflow.current_version.id not in existing_version_ids
                ):
                    await self._repos.versions.insert(workflow.current_version, conn=conn)
                if workflow.last_gate_run is not None:
                    await self._repos.gate_runs.insert(workflow.last_gate_run, conn=conn)
                for item in persisted.values():
                    await self._repos.items.update(item, expected_revision=item_revision, conn=conn)
        except StaleRevisionError as exc:
            raise ScriptAuthoringError("stale_revision", str(exc)) from exc

    def _raise_llm_unavailable(self) -> NoReturn:
        raise ScriptAuthoringError("llm_unavailable", "AI generation is not available yet (B5/B6)")

    # ── ScriptSet aggregate (task 11.2) ──────────────────────────────

    async def create_script_set(
        self,
        *,
        name: str,
        transition_policy: str,
        product_ids: list[str],
        brief: dict[str, Any] | None,
    ) -> dict[str, Any]:
        brief_dict = brief or {}
        script_set = ScriptSet(
            id=new_id("script_set"),
            shop_id=brief_dict.get("shop_name") or "default",
            title=name,
            brief=self._brief_from_dict(brief_dict, transition_policy),
            product_ids=list(product_ids),
            revision=0,
        )
        items = [
            ScriptItem(id=new_id("script_item"), script_set_id=script_set.id, product_id=pid)
            for pid in script_set.product_ids
        ]
        async with self._repos.transaction() as conn:
            await self._repos.script_sets.insert(script_set, conn=conn)
            for item in items:
                await self._repos.items.insert(item, conn=conn)
        return self._set_wire(script_set, items)

    async def get_script_set(self, *, set_id: str) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            return None
        items = await self._repos.items.list_by_set(set_id)
        return self._set_wire(script_set, items)

    async def update_script_set(
        self,
        *,
        set_id: str,
        name: str | None,
        transition_policy: str | None,
        product_ids: list[str] | None,
        brief: dict[str, Any] | None,
        revision: int | None,
    ) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            return None
        if revision is not None and revision != script_set.revision:
            raise ScriptAuthoringError("stale_revision", f"script set {set_id} revision mismatch")
        if name is not None:
            script_set.title = name
        if transition_policy is not None:
            script_set.brief.transition_policy = transition_policy
        if brief is not None:
            script_set.brief = self._brief_from_dict(brief, script_set.brief.transition_policy)
        existing_pids = set(script_set.product_ids)
        if product_ids is not None:
            seen: set[str] = set()
            deduped: list[str] = []
            for pid in product_ids:
                if pid not in seen:
                    seen.add(pid)
                    deduped.append(pid)
            script_set.product_ids = deduped
        try:
            async with self._repos.transaction() as conn:
                await self._repos.script_sets.update(
                    script_set, expected_revision=script_set.revision, conn=conn
                )
                if product_ids is not None:
                    known = set(existing_pids)
                    for pid in script_set.product_ids:
                        if pid not in known:
                            item = ScriptItem(
                                id=new_id("script_item"),
                                script_set_id=script_set.id,
                                product_id=pid,
                            )
                            await self._repos.items.insert(item, conn=conn)
                            known.add(pid)
        except StaleRevisionError as exc:
            raise ScriptAuthoringError("stale_revision", str(exc)) from exc
        script_set.revision += 1
        items = await self._repos.items.list_by_set(set_id)
        return self._set_wire(script_set, items)

    # ── Per-product commands (task 11.3) ─────────────────────────────

    async def save_draft(
        self,
        *,
        set_id: str,
        product_id: str,
        display_text: str,
        spoken_text: str | None,
        revision: int | None,
    ) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        if revision is not None and revision != script_set.revision:
            raise ScriptAuthoringError("stale_revision", f"script set {set_id} revision mismatch")
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        current_version = None
        if item.current_version_id is not None:
            current_version = await self._repos.versions.get(item.current_version_id)
        spoken = (
            spoken_text
            if spoken_text is not None
            else compile_spoken_text(display_text).spoken_text
        )
        bridge = _SyncPersistBridge()
        workflow = self._make_workflow(
            item, current_version, bridge, script_set.brief.transition_policy
        )
        try:
            workflow.create_manual_draft(display_text=display_text, spoken_text=spoken)
        except IllegalTransitionError as exc:
            raise ScriptAuthoringError("illegal_transition", str(exc)) from exc
        existing = {current_version.id} if current_version is not None else set()
        await self._persist_workflow(
            workflow, bridge, item_revision=item.revision, existing_version_ids=existing
        )
        return {"ok": True, "product_id": product_id, "state": item.state.name}

    async def submit_for_gate(self, *, set_id: str, product_id: str) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        if item.current_version_id is None:
            raise ScriptAuthoringError("illegal_transition", "cannot submit without a draft")
        current_version = await self._repos.versions.get(item.current_version_id)
        if current_version is None:
            raise ScriptAuthoringError("illegal_transition", "current draft version is missing")
        bridge = _SyncPersistBridge()
        workflow = self._make_workflow(
            item, current_version, bridge, script_set.brief.transition_policy
        )
        try:
            result = workflow.submit()
        except IllegalTransitionError as exc:
            raise ScriptAuthoringError("illegal_transition", str(exc)) from exc
        if workflow.last_gate_run is not None and workflow.last_gate_run.script_version_id is None:
            workflow.last_gate_run.script_version_id = current_version.id
        await self._persist_workflow(
            workflow, bridge, item_revision=item.revision, existing_version_ids={current_version.id}
        )
        return {
            "ok": True,
            "product_id": product_id,
            "state": item.state.name,
            "gate": self._gate_wire(result),
        }

    # ── generation preview (task 11.4) ───────────────────────────────

    async def preview_product(
        self, *, set_id: str, product_id: str, target_duration_s: int
    ) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        try:
            preview = compute_product_preview(product_id, target_duration_s, self._calibration())
        except GenerationBudgetError as exc:
            raise ScriptAuthoringError("illegal_transition", str(exc)) from exc
        return {
            "product_id": preview.product_id,
            "target_duration_s": int(preview.target_duration_s),
            "planned_segment_count": preview.planned_segment_count,
            "estimated_semantic_calls": preview.estimated_semantic_calls,
        }

    # ── human approval (task 11.7) ───────────────────────────────────

    async def approve_product(
        self,
        *,
        set_id: str,
        product_id: str,
        version_id: str,
        actor: str,
    ) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        if item.state is not ScriptState.REVIEWABLE:
            raise ScriptAuthoringError(
                "illegal_transition", "only a REVIEWABLE version is approvable"
            )
        if item.current_version_id != version_id:
            raise ScriptAuthoringError(
                "illegal_transition", "only the current REVIEWABLE version is approvable"
            )
        version = await self._repos.versions.get(version_id)
        if version is None:
            self._raise_not_found("script version", version_id)

        context = ScriptGateContext(
            transition_policy=script_set.brief.transition_policy, facts=ProductFacts()
        )
        result = self._gate.run_full_script([version.spoken_text], context)  # exactly once
        run = GateRun(
            id=new_id("gate_run"),
            script_item_id=item.id,
            full=True,
            passed=result.passed,
            violations=[_to_gate_violation(v) for v in result.violations],
            rule_set_fingerprint=result.fingerprint.hexdigest,
            script_version_id=version.id,
        )
        request = ApprovalRequest(
            script_item_id=item.id,
            script_version_id=version.id,
            compiled_spoken_text=version.spoken_text,
            segment_version_ids=tuple(version.segment_version_ids),
            plan_version=version.plan_version,
            rule_set_key=rule_set_version_key(result.fingerprint),
            product_facts_version="",
            promotion_version="",
            persona_brief_version="",
            actor_id=actor,
            is_human=True,
            authorized=True,
        )
        try:
            record = approve_script(
                request,
                gate_checker=_ApprovalGateChecker(run.id, result),
                new_approval_id=new_id("approval"),
            )
        except ApprovalError as exc:
            raise ScriptAuthoringError("illegal_transition", str(exc)) from exc
        approval = Approval(
            id=record.id,
            script_item_id=item.id,
            script_version_id=version.id,
            actor=actor,
            approval_hash=record.approval_hash,
            gate_run_id=run.id,
        )
        bridge = _SyncPersistBridge()
        workflow = self._make_workflow(item, version, bridge, script_set.brief.transition_policy)
        try:
            workflow.approve(actor=actor)
        except IllegalTransitionError as exc:
            raise ScriptAuthoringError("illegal_transition", str(exc)) from exc
        try:
            async with self._repos.transaction() as conn:
                await self._repos.items.update(item, expected_revision=item.revision, conn=conn)
                await self._repos.gate_runs.insert(run, conn=conn)
                await self._repos.approvals.insert(
                    approval,
                    dependencies=_approval_dependencies_dict(record.dependencies),
                    conn=conn,
                )
        except StaleRevisionError as exc:
            raise ScriptAuthoringError("stale_revision", str(exc)) from exc
        return {
            "ok": True,
            "product_id": product_id,
            "state": "APPROVED",
            "approval": {
                "version_id": version.id,
                "actor": actor,
                "approved_at": approval.created_at,
            },
        }

    async def approve_batch(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        version_ids: dict[str, str],
        actor: str,
    ) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        approvals: dict[str, Any] = {}
        for pid in product_ids:
            version_id = version_ids.get(pid)
            if version_id is None:
                raise ScriptAuthoringError("illegal_transition", f"missing version_id for {pid}")
            approvals[pid] = await self.approve_product(
                set_id=set_id, product_id=pid, version_id=version_id, actor=actor
            )
        return {"ok": True, "approvals": approvals}

    # ── AI generation / batch (B5/B6) ────────────────────────────────

    async def start_generation(
        self,
        *,
        set_id: str,
        product_id: str,
        target_duration_s: int,
        intent: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._raise_llm_unavailable()

    async def regenerate_segment(
        self,
        *,
        set_id: str,
        product_id: str,
        segment_index: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._raise_llm_unavailable()

    async def fix_with_ai(
        self,
        *,
        set_id: str,
        product_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._raise_llm_unavailable()

    async def start_batch_generation(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        target_duration_s: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._raise_llm_unavailable()

    async def get_batch(self, *, set_id: str, batch_id: str) -> dict[str, Any] | None:
        self._raise_llm_unavailable()

    async def cancel_batch(self, *, set_id: str, batch_id: str) -> dict[str, Any] | None:
        self._raise_llm_unavailable()

    async def get_batch_events_snapshot(self, *, set_id: str, batch_id: str) -> str | None:
        self._raise_llm_unavailable()

    async def stream_batch_events(
        self, *, set_id: str, batch_id: str
    ) -> AsyncIterator[dict[str, str]]:
        self._raise_llm_unavailable()
        yield  # pragma: no cover - unreachable; keeps this an async generator
