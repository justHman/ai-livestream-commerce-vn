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

import asyncio
import json
import os
import queue
import re
import socket
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
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
from backend.application.script_authoring.generation.batch import (
    BatchOrchestratorConfig,
    BatchRequest,
    BatchScriptGenerationOrchestrator,
    BatchState,
    IdempotencyRegistry,
    recover_batch,
    request_fingerprint,
)
from backend.application.script_authoring.generation.calibration import (
    GenerationBudgetCalibration,
    GenerationBudgetError,
)
from backend.application.script_authoring.generation.continuity import ContinuityState
from backend.application.script_authoring.generation.context_builder import (
    AuthoritativeContext,
)
from backend.application.script_authoring.generation.driver import WorkflowDriver
from backend.application.script_authoring.generation.intent import (
    ScriptIntent as GenScriptIntent,
    build_transition_context,
)
from backend.application.script_authoring.generation.planner import (
    AuthoritativeContext as PlannerAuthoritativeContext,
    PlanRejectionError,
    ProductScriptPlanner,
)
from backend.application.script_authoring.generation.preview import (
    preview_product as compute_product_preview,
)
from backend.application.script_authoring.generation.prompt_builder import (
    build_generate_prompt,
    build_repair_prompt,
)
from backend.application.script_authoring.generation.segment_generator import (
    SegmentGenerationResult,
    SegmentStepOutcome,
)
from backend.application.script_authoring.generation.skill_loader import SkillLoader
from backend.application.script_authoring.models import (
    Approval,
    GateRun,
    GateViolation,
    GenerationBatch,
    GenerationBatchStatus,
    GenerationFingerprint,
    GenerationJob,
    GenerationJobStatus,
    LiveSessionBrief,
    ProductScriptPlan,
    ScriptIntent,
    ScriptItem,
    ScriptSegment,
    ScriptSet,
    ScriptSource,
    ScriptState,
    ScriptVersion,
    new_id,
)
from backend.application.script_authoring.repositories import (
    LeaseLostError,
    PostgresAuthoringRepositories,
    StaleRevisionError,
)
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.state import IllegalTransitionError
from backend.application.script_authoring.workflow import (
    InvalidFixStateError,
    ProductGenerationWorkflow,
)
from backend.config import ScriptAuthoringConfig

__all__ = ["ScriptAuthoringServiceImpl"]


class _SyncPersistBridge:
    """Thread-safe sink for the FSM's synchronous ``persist`` hook.

    The FSM calls ``persist(item)`` after every persisted change; the service
    drains the queue once per command and writes the collected artifacts
    (items / plans / segments / versions / gate runs) inside a single
    repository transaction. Thread-safe so a background driver (B6) can
    enqueue from a worker while the async loop drains.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue()

    def __call__(self, artifact: Any) -> None:
        self._queue.put(artifact)

    def drain(self) -> list[Any]:
        artifacts: list[Any] = []
        while True:
            try:
                artifacts.append(self._queue.get_nowait())
            except queue.Empty:
                return artifacts


class _EventEmittingDriver:
    """WorkflowDriver proxy that emits fine-grained SSE phase events.

    The batch orchestrator drives the driver protocol (``step`` /
    ``is_terminal`` / ``snapshot`` / ``restore``) but only knows product-level
    lifecycle; this proxy derives the contract's ``product.planning_started`` /
    ``product.plan_ready`` / ``segment.gate_passed`` / ``product.reviewable`` /
    ``product.failed`` events from the FSM state changes of each step. For the
    single-product path ``emit`` is a no-op so the wrapper is inert.
    """

    def __init__(self, driver: WorkflowDriver, emit, batch_id) -> None:
        self._driver = driver
        self._emit = emit
        # batch_id may be a fixed string or a callable (the batch orchestrator
        # generates its own batch id during start(); callers resolve lazily).
        self._batch_id = batch_id
        self.product_id = driver.product_id

    def _bid(self) -> str:
        return self._batch_id() if callable(self._batch_id) else self._batch_id

    def step(self) -> bool:
        wf = self._driver.workflow
        before = wf.item.state
        segments_before = len(wf.segments)
        more = self._driver.step()
        after = wf.item.state
        pid = self.product_id
        bid = self._bid()
        if before is ScriptState.EMPTY and after is ScriptState.PLANNING:
            self._emit(
                "product.planning_started",
                {"batch_id": bid, "product_id": pid},
            )
        elif before is ScriptState.PLANNING and after is ScriptState.GENERATING:
            self._emit(
                "product.plan_ready",
                {
                    "batch_id": bid,
                    "product_id": pid,
                    "plan_segment_count": wf.plan_segment_count,
                },
            )
        elif (
            before is ScriptState.GENERATING
            and after is ScriptState.GENERATING
            and len(wf.segments) > segments_before
        ):
            self._emit(
                "segment.gate_passed",
                {
                    "batch_id": bid,
                    "product_id": pid,
                    "segment_index": len(wf.segments) - 1,
                },
            )
        elif after is ScriptState.REVIEWABLE:
            self._emit("product.reviewable", {"batch_id": bid, "product_id": pid})
        elif after in (ScriptState.GATE_FAILED, ScriptState.FAILED):
            self._emit("product.failed", {"batch_id": bid, "product_id": pid})
        return more

    def is_terminal(self) -> bool:
        return self._driver.is_terminal()

    def snapshot(self) -> dict:
        return self._driver.snapshot()

    def restore(self, state: dict) -> None:
        self._driver.restore(state)

    @property
    def workflow(self):
        return self._driver.workflow


class _RegenWorkflow:
    """Minimal workflow-shaped holder for the manual regenerate failure path."""

    def __init__(self, item: ScriptItem) -> None:
        self.item = item
        self.last_error: str | None = None


class DbIdempotencyRegistry(IdempotencyRegistry):
    """``IdempotencyRegistry`` backed by the SQL idempotency table (task 10.6).

    The orchestrator's registry protocol is synchronous, so this class reads
    from the injected in-memory store and asynchronously flushes ``register``
    to Postgres. The service seeds the store from the DB before starting a
    batch and performs an explicit synchronous register before returning, so
    the mapping survives restart (``ON CONFLICT DO NOTHING`` keeps first-wins).
    """

    def __init__(self, repo, store: dict | None = None) -> None:
        super().__init__(store=store)
        self._repo = repo

    def register(self, fingerprint: str, batch_id: str) -> None:
        super().register(fingerprint, batch_id)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._flush(fingerprint, batch_id))

    async def _flush(self, fingerprint: str, batch_id: str) -> None:
        try:
            await self._repo.register(fingerprint, batch_id)
        except Exception:
            pass  # the service performs an explicit synchronous register too


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
    """Concrete ``ScriptAuthoringService`` over SQL repositories (Change B).

    B4 implemented the manual zero-LLM core; B6 replaces the AI-method stubs
    with real long-form generation (one product), segment regeneration, AI fix,
    and multi-product batch orchestration over the injected ``EngineManager``.
    When ``engine_manager`` is None (or the LLM is unavailable) the four AI
    commands raise ``llm_unavailable``; batch reads / SSE never do.
    """

    def __init__(
        self,
        repos: PostgresAuthoringRepositories,
        *,
        config: ScriptAuthoringConfig | None = None,
        gate: ScriptGate | None = None,
        engine_manager: Any | None = None,
    ) -> None:
        self._repos = repos
        self._config = config or ScriptAuthoringConfig()
        self._gate = gate or self._default_gate()
        self._engine_manager = engine_manager
        # Owned background-job tasks (HIGH-1). Every fire-and-forget job created
        # by the AI commands goes through ``_spawn`` and is registered here so
        # the lifespan drain can quiesce it BEFORE the repository pool closes.
        # ``_accepting_jobs`` drops to False during shutdown so no new work can
        # race the close.
        self._tasks: set[asyncio.Task] = set()
        self._accepting_jobs = True
        # In-process batch orchestrators keyed by batch id (B6) so cancel_batch
        # finds a running batch; otherwise it recovers from persisted state.
        self._active_orchestrators: dict[str, BatchScriptGenerationOrchestrator] = {}
        self._batch_persist_queues: dict[str, queue.Queue[BatchState]] = {}
        self._batch_artifact_bridges: dict[str, _SyncPersistBridge] = {}
        self._event_rings: dict[str, deque] = {}
        # Stable per-process identity for the durable recovery lease (HIGH-1):
        # unique across replicas, constant for the lifetime of this instance.
        # A random uuid is unique per replica AND distinguishes two services
        # created in the same OS process (integration tests), unlike host:pid.
        self._instance_id: str = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        # Job/batch rows this process currently holds the DB lease on (HIGH-1),
        # used by drain() to release them promptly on graceful shutdown.
        self._leased_jobs: dict[str, int] = {}  # job id -> lease epoch
        self._leased_batches: dict[str, int] = {}  # batch id -> lease epoch

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
    def _version_wire(version: ScriptVersion | None) -> dict[str, Any] | None:
        if version is None:
            return None
        return {
            "id": version.id,
            "version": version.version,
            "source": version.source.value
            if hasattr(version.source, "value")
            else str(version.source),
            "display_text": version.display_text,
            "spoken_text": version.spoken_text,
            "gate_result": version.gate_run_id,
            "created_at": version.created_at,
        }

    @staticmethod
    def _set_wire(
        script_set: ScriptSet,
        items: list[ScriptItem],
        *,
        versions_by_id: dict[str, ScriptVersion] | None = None,
        gate_by_item: dict[str, dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        versions_by_id = versions_by_id or {}
        gate_by_item = gate_by_item or {}
        out_items: dict[str, Any] = {}
        for item in items:
            cv = versions_by_id.get(item.current_version_id) if item.current_version_id else None
            out_items[item.product_id] = {
                "state": item.state.name,
                "current_version_id": item.current_version_id,
                "approved_version_id": item.approved_version_id,
                "current_version": ScriptAuthoringServiceImpl._version_wire(cv),
                "gate": gate_by_item.get(item.id),
            }
        return {
            "id": script_set.id,
            "name": script_set.title,
            "transition_policy": script_set.brief.transition_policy,
            "product_ids": list(script_set.product_ids),
            "revision": script_set.revision,
            "items": out_items,
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
        job: GenerationJob | None = None,
    ) -> None:
        """Write workflow-persisted items + new version / gate run in one tx.

        Fencing (R8.2): when called from an owned execution path (``job``), the
        transaction begins with ``assert_and_renew_lease`` so the artifact
        writes and the lease assertion share ONE transaction — a stale owner
        whose lease was taken over observes ``LeaseLostError`` and commits
        nothing.
        """
        # The FSM persists the same item object after each transition; dedupe
        # by id so the optimistic-lock UPDATE runs exactly once.
        persisted: dict[str, ScriptItem] = {}
        for item in bridge.drain():
            persisted[item.id] = item
        try:
            async with self._repos.transaction() as conn:
                if job is not None and job.lease_owner is not None:
                    await self._repos.jobs.assert_and_renew_lease(
                        job.id,
                        job.lease_owner,
                        job.lease_epoch,
                        self._config.recovery_lease_seconds,
                        conn=conn,
                    )
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
        raise ScriptAuthoringError("llm_unavailable", "LLM engine is not available")

    def _require_llm(self) -> Callable[[str], str]:
        """Return the sync ``(text) -> str`` LLM callable or raise 503.

        Decision 1: unavailable when there is no engine manager, the engine is
        ``none``/empty, the last load failed, or no callable is bound.
        """
        em = self._engine_manager
        if em is None:
            self._raise_llm_unavailable()
        if em.llm_cfg.get("engine") in ("none", "", None):
            self._raise_llm_unavailable()
        if em.llm_failed:
            self._raise_llm_unavailable()
        fn = em.get_llm_fn()
        if fn is None:
            self._raise_llm_unavailable()
        return fn

    def _safe_llm(self) -> Callable[[str], str] | None:
        """Non-raising ``_require_llm`` for recovery paths (returns None)."""
        try:
            return self._require_llm()
        except ScriptAuthoringError:
            return None

    def _require_accepting(self) -> None:
        """Refuse new AI background jobs during shutdown (HIGH-1 admission stop)."""
        if not self._accepting_jobs:
            raise ScriptAuthoringError("service_unavailable", "script authoring is shutting down")

    def _spawn(self, coro, *, name: str) -> asyncio.Task:
        """Own a background-job task so the lifespan drain can quiesce it.

        Every fire-and-forget job (generation / regenerate / fix / batch) is
        created through this helper; ``drain`` waits for or cancels each owned
        task BEFORE ``repos.close()`` so no task can race the pool close
        (HIGH-1). Also the admission-stop safety net: it refuses to start when
        ``_accepting_jobs`` is False, even if a command already did DB work.
        """
        self._require_accepting()
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _owns_task(self, name: str) -> bool:
        """True when this service owns a still-active task named ``name``.

        In-process duplicate-runner guard for recovery (HIGH-B / §9): startup
        recovery must not spawn a second runner for a job/batch this process is
        already driving.
        """
        return any(t.get_name() == name and not t.done() for t in self._tasks)

    def _job_lease(self, job_id: str) -> tuple[str, int] | None:
        """Return ``(owner, epoch)`` for a job this process holds the lease on."""
        epoch = self._leased_jobs.get(job_id)
        return (self._instance_id, epoch) if epoch is not None else None

    def _batch_lease(self, batch_id: str) -> tuple[str, int] | None:
        """Return ``(owner, epoch)`` for a batch this process holds the lease on."""
        epoch = self._leased_batches.get(batch_id)
        return (self._instance_id, epoch) if epoch is not None else None

    async def _with_lease_heartbeat(
        self,
        fn: Callable[[], Any],
        *,
        job: GenerationJob | None = None,
        batch_id: str | None = None,
        batch_lease: tuple[str, int] | None = None,
    ) -> Any:
        """Run a sync provider call off the loop under a bounded lease heartbeat.

        R8.3: a HEALTHY provider call can outlive the lease window
        (``recovery_lease_seconds``). While ``await asyncio.to_thread(fn)`` is
        in flight this renews the job/batch fence every
        ``lease_heartbeat_interval()`` seconds via ``assert_and_renew_lease``
        (owner+epoch matched) so a slow-but-alive owner is not falsely taken
        over by a recovering replica. If the fence is lost mid-call the result
        is discarded and ``LeaseLostError`` is raised — the caller then commits
        NO artifacts and lets the new lease owner continue.

        Bounded + self-cleaning: the heartbeat task is cancelled and awaited
        before this returns (even on cancellation or provider failure), so no
        dangling task ever survives a completed ``to_thread``.
        """
        interval = self._config.lease_heartbeat_interval()
        lease_s = self._config.recovery_lease_seconds
        lost = asyncio.Event()

        async def _beat() -> None:
            try:
                while True:
                    await asyncio.sleep(interval)
                    try:
                        if job is not None and job.lease_owner is not None:
                            await self._repos.jobs.assert_and_renew_lease(
                                job.id, job.lease_owner, job.lease_epoch, lease_s
                            )
                        elif batch_id is not None and batch_lease is not None:
                            owner, epoch = batch_lease
                            await self._repos.batches.assert_and_renew_lease(
                                batch_id, owner, epoch, lease_s
                            )
                    except LeaseLostError:
                        lost.set()
                        return
            except asyncio.CancelledError:
                return

        task = asyncio.create_task(
            _beat(), name=f"sa-heartbeat:{job.id if job is not None else batch_id}"
        )
        try:
            result = await asyncio.to_thread(fn)
        except BaseException:
            # Cancellation or provider failure: cancel the heartbeat, then
            # re-raise the ORIGINAL exception (traceback preserved).
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise
        else:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if lost.is_set():
                raise LeaseLostError("lease lost while provider work was in flight")
            return result

    # ── B6 generation helpers ──────────────────────────────────────────

    def _event_ring(self, batch_id: str) -> deque:
        ring = self._event_rings.get(batch_id)
        if ring is None:
            # Retention window is seconds; bound the ring to a sane event cap.
            maxlen = max(64, self._config.sse_retention_seconds // 10)
            ring = deque(maxlen=maxlen)
            self._event_rings[batch_id] = ring
        return ring

    @staticmethod
    def _map_batch_event(event: str) -> str | None:
        """Map orchestrator events to the SSE contract (task 11.10).

        ``product.started``/``product.completed`` are dropped — the workflow
        driver emits the finer-grained ``product.*``/``segment.*`` events.
        """
        if event in ("product.started", "product.completed"):
            return None
        return {
            "batch.started": "batch.progress",
            "product.failed": "product.failed",
            "batch.completed": "batch.completed",
            "batch.partial_completed": "batch.completed",
            "batch.failed": "batch.error",
            "batch.cancelled": "batch.cancelled",
        }.get(event, event)

    def _batch_sink(self, batch_id: str, ring: deque, event: str, payload: dict) -> None:
        mapped = self._map_batch_event(event)
        if mapped is None:
            return
        ring.append({"event": mapped, "data": json.dumps(payload)})

    @staticmethod
    def _job_status_for(state: ScriptState) -> GenerationJobStatus:
        if state in (ScriptState.REVIEWABLE, ScriptState.APPROVED):
            return GenerationJobStatus.COMPLETED
        if state in (ScriptState.FAILED, ScriptState.GATE_FAILED, ScriptState.CANCELLED):
            return GenerationJobStatus.FAILED
        return GenerationJobStatus.RUNNING

    def _skill_loader(self) -> SkillLoader:
        path = self._config.skill_path
        if path:
            try:
                return SkillLoader(Path(path))
            except Exception:
                pass
        return SkillLoader()

    def _skill_text(self) -> str:
        return self._skill_loader().content()

    def _generation_fingerprint(self) -> GenerationFingerprint:
        em = self._engine_manager
        model = em.llm_cfg.get("model", "") if em is not None else ""
        skill_version = self._config.expected_skill_version
        try:
            skill_version = self._skill_loader().skill_version() or skill_version
        except Exception:
            pass
        return GenerationFingerprint(model=model, skill_version=skill_version)

    def _model_fingerprint(self) -> str:
        em = self._engine_manager
        if em is None:
            return ""
        model = em.llm_cfg.get("model", "") or ""
        return f"{em.llm_cfg.get('engine', '')}:{model}"

    def _make_loaders(self, items, segments, versions):
        def load_item(item_id: str) -> ScriptItem:
            for item in items.values():
                if item.id == item_id:
                    return item
            raise KeyError(item_id)

        def load_segment(segment_id: str) -> ScriptSegment:
            segment = segments.get(segment_id)
            if segment is None:
                raise KeyError(segment_id)
            return segment

        def load_version(version_id: str) -> ScriptVersion:
            version = versions.get(version_id)
            if version is None:
                raise KeyError(version_id)
            return version

        return load_item, load_segment, load_version

    def _build_planning_prompt(
        self, skill_text: str, item: ScriptItem, target_duration_s: int
    ) -> str:
        return "\n".join(
            [
                "TASK: PLAN_THE_SCRIPT_SEGMENTS",
                skill_text.strip() or "(no sales skill provided)",
                f"Plan a Vietnamese livestream sales script for product {item.product_id} "
                f"with a target spoken duration of {target_duration_s} seconds.",
                "Output exactly one section per line in this format:",
                "<index>. <title>|<intent>|<target_duration_s>",
                "Return only the plan lines.",
            ]
        )

    def _parse_plan_sections(self, raw: str) -> list[dict]:
        """Parse the model's plan lines into ``{title, intent, target_duration_s}``.

        Lines that do not match the ``title|intent|duration`` shape are skipped;
        an entirely unparseable response yields an empty list, which the
        ``plan_generate`` closure turns into a deterministic failure.
        """
        sections: list[dict] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) != 3:
                continue
            title = re.sub(r"^\d+\s*[.:-]\s*", "", parts[0])
            intent = parts[1]
            try:
                duration = int(parts[2])
            except ValueError:
                continue
            if not title or not intent or duration <= 0:
                continue
            sections.append({"title": title, "intent": intent, "target_duration_s": duration})
        return sections

    def _make_plan_generate(self, item, script_set, target_duration_s, llm_fn, *, emit):
        planner = ProductScriptPlanner(self._calibration())
        skill_text = self._skill_text()

        def plan_generate():
            prompt = self._build_planning_prompt(skill_text, item, target_duration_s)
            raw = llm_fn(prompt)
            sections = self._parse_plan_sections(raw)
            if not sections:
                raise PlanRejectionError("no parseable plan sections in model output")
            plan = planner.plan(
                item.product_id,
                float(target_duration_s),
                PlannerAuthoritativeContext(
                    product_id=item.product_id, fact_ids=frozenset(), objection_ids=frozenset()
                ),
                sections,
            )
            candidates = [
                {
                    "title": s.topic,
                    "intent": s.intent,
                    "target_duration_s": int(s.target_duration_s),
                }
                for s in plan.segments
            ]
            return len(candidates), candidates

        return plan_generate

    def _make_segment_generate(self, item, script_set, target_duration_s, llm_fn, *, emit):
        skill_text = self._skill_text()
        transition = build_transition_context(script_set.brief.transition_policy)
        gen_intent = GenScriptIntent(intent="selling", target_duration_s=target_duration_s)

        def segment_generate(index: int, continuity: ContinuityState) -> SegmentStepOutcome:
            emit("segment.started", {"product_id": item.product_id, "segment_index": index})
            parts = build_generate_prompt(
                skill_text,
                generation_constraints=[],
                context=AuthoritativeContext(),
                duration_s=target_duration_s,
                intent=gen_intent,
                transition=transition,
                segment_index=index,
                continuity=continuity,
            )
            prompt = "\n\n".join(filter(None, [parts.system, parts.context, parts.user]))
            raw = llm_fn(prompt)
            if not raw or not raw.strip():
                return SegmentStepOutcome(index=index, state=continuity, error="empty model output")
            return SegmentStepOutcome(
                index=index,
                state=continuity,
                result=SegmentGenerationResult(
                    segment_index=index, display_text=raw, spoken_text=raw
                ),
            )

        return segment_generate

    def _build_driver(
        self,
        item: ScriptItem,
        script_set: ScriptSet,
        target_duration_s: int,
        llm_fn: Callable[[str], str] | None,
        bridge: _SyncPersistBridge,
        *,
        emit,
        batch_id: str,
        loaders,
    ):
        context = ScriptGateContext(
            transition_policy=script_set.brief.transition_policy, facts=ProductFacts()
        )

        def segment_gate(text: str) -> GateRunResult:
            return self._gate.run_segment(text, context)

        def full_gate(segments: Sequence[str]) -> GateRunResult:
            return self._gate.run_full_script(list(segments), context)

        workflow = ProductGenerationWorkflow(
            item=item,
            segment_gate=segment_gate,
            full_gate=full_gate,
            persist=bridge,
            generate=lambda *a, **k: None,  # AI-enabled marker; driver calls closures
        )
        workflow.target_duration_s = target_duration_s
        plan_generate = self._make_plan_generate(
            item, script_set, target_duration_s, llm_fn, emit=emit
        )
        segment_generate = self._make_segment_generate(
            item, script_set, target_duration_s, llm_fn, emit=emit
        )
        driver = WorkflowDriver(
            product_id=item.product_id,
            workflow=workflow,
            plan_generate=plan_generate,
            segment_generate=segment_generate,
            persist=bridge,
            load_item=loaders[0],
            load_segment=loaders[1],
            load_version=loaders[2],
            fingerprint=self._generation_fingerprint(),
        )
        return _EventEmittingDriver(driver, emit, batch_id)

    async def _drain_artifacts(
        self,
        bridge: _SyncPersistBridge,
        revisions: dict[str, int],
        existing_versions: set[str],
        *,
        job: GenerationJob | None = None,
        batch_id: str | None = None,
        batch_lease: tuple[str, int] | None = None,
    ) -> None:
        """Persist one drain of driver artifacts in a single transaction.

        Fencing (R8.2): when the drain belongs to an owned execution path
        (``job`` or ``batch_id``+``batch_lease``), the transaction begins with
        ``assert_and_renew_lease`` so the artifact writes and the lease
        assertion share ONE transaction — a stale owner whose lease was taken
        over observes ``LeaseLostError`` and commits ZERO artifacts.

        Gotcha (a): a ``ProductScriptPlan``'s embedded placeholder segments are
        NOT inserted — only the plan row is written (deep copy with
        ``segments=[]``); the real content segments are inserted separately by
        the drain, avoiding the ``(plan_id, segment_index, version)`` unique
        violation. Order: lease fence -> plan -> segments -> gate runs ->
        versions -> item.
        """
        artifacts = bridge.drain()
        if not artifacts:
            return
        items: dict[str, ScriptItem] = {}
        plans: list[ProductScriptPlan] = []
        segments: list[ScriptSegment] = []
        versions: list[ScriptVersion] = []
        gate_runs: list[GateRun] = []
        for artifact in artifacts:
            if isinstance(artifact, ScriptItem):
                items[artifact.id] = artifact
            elif isinstance(artifact, ProductScriptPlan):
                plans.append(artifact)
            elif isinstance(artifact, ScriptSegment):
                segments.append(artifact)
            elif isinstance(artifact, ScriptVersion):
                versions.append(artifact)
            elif isinstance(artifact, GateRun):
                gate_runs.append(artifact)
        if not (items or plans or segments or versions or gate_runs):
            return
        try:
            async with self._repos.transaction() as conn:
                if job is not None and job.lease_owner is not None:
                    await self._repos.jobs.assert_and_renew_lease(
                        job.id,
                        job.lease_owner,
                        job.lease_epoch,
                        self._config.recovery_lease_seconds,
                        conn=conn,
                    )
                elif batch_id is not None and batch_lease is not None:
                    owner, epoch = batch_lease
                    await self._repos.batches.assert_and_renew_lease(
                        batch_id,
                        owner,
                        epoch,
                        self._config.recovery_lease_seconds,
                        conn=conn,
                    )
                for plan in plans:
                    await self._repos.plans.insert(
                        plan.model_copy(update={"segments": []}), conn=conn
                    )
                for segment in segments:
                    await self._repos.segments.insert(segment, conn=conn)
                for run in gate_runs:
                    await self._repos.gate_runs.insert(run, conn=conn)
                for version in versions:
                    if version.id not in existing_versions:
                        await self._repos.versions.insert(version, conn=conn)
                        existing_versions.add(version.id)
                for item in items.values():
                    expected = revisions.get(item.id)
                    if expected is None:
                        continue
                    await self._repos.items.update(item, expected_revision=expected, conn=conn)
                    revisions[item.id] = expected + 1
        except StaleRevisionError as exc:
            raise ScriptAuthoringError("stale_revision", str(exc)) from exc

    async def _create_job(
        self,
        item: ScriptItem,
        set_id: str,
        product_id: str,
        intent: ScriptIntent,
        target_duration_s: int,
        idempotency_key: str,
    ) -> tuple[GenerationJob, int]:
        """Insert a minimal batch row + the job row, claiming the job's
        recovery lease in the SAME transaction (HIGH-1: no window where a
        concurrent ``recover_pending`` on another replica could claim a job
        this process just created). Returns ``(job, lease_epoch)``."""
        batch = GenerationBatch(
            id=new_id("batch"),
            script_set_id=set_id,
            status=GenerationBatchStatus.QUEUED,
            product_ids=[product_id],
            job_ids=[],
            estimated_semantic_calls=0,
            idempotency_key=idempotency_key,
        )
        batch_state = BatchState(
            batch_id=batch.id,
            script_set_id=set_id,
            status="queued",
            requested_products=[product_id],
        )
        job = GenerationJob(
            id=new_id("job"),
            batch_id=batch.id,
            script_item_id=item.id,
            product_id=product_id,
            intent=intent,
            status=GenerationJobStatus.RUNNING,
            target_duration_s=target_duration_s,
            fingerprint=self._generation_fingerprint(),
            idempotency_key=idempotency_key,
        )
        async with self._repos.transaction() as conn:
            await self._repos.batches.insert(batch, state=batch_state, conn=conn)
            await self._repos.jobs.insert(job, conn=conn)
            lease_epoch = await self._repos.jobs.acquire_lease(
                job.id, self._instance_id, self._config.recovery_lease_seconds, conn=conn
            )
        # Carry the acquired fence on the model so the drive loop's progress
        # writes pass the same (owner, epoch) to ``jobs.update``.
        job.lease_owner = self._instance_id
        job.lease_epoch = lease_epoch
        self._leased_jobs[job.id] = lease_epoch
        return job, lease_epoch

    async def _update_job(self, job: GenerationJob, workflow, status: GenerationJobStatus) -> None:
        job.status = status
        job.plan_id = workflow.plan_id
        job.plan_segment_count = workflow.plan_segment_count
        job.current_segment_index = workflow.current_segment_index
        await self._repos.jobs.update(
            job,
            lease_owner=job.lease_owner,
            lease_epoch=job.lease_epoch,
            lease_duration_s=self._config.recovery_lease_seconds,
        )

    @staticmethod
    def _land_failed(workflow, message: str, bridge: _SyncPersistBridge) -> None:
        workflow.item.state = ScriptState.FAILED
        workflow.last_error = message
        bridge(workflow.item)

    async def _drain_batch_persists(
        self, batch_id: str, persist_queue: queue.Queue[BatchState]
    ) -> None:
        """Persist queued ``BatchState`` snapshots with a live revision guard.

        The orchestrator bumps ``BatchState.revision`` only on ``start`` /
        ``cancel`` while the SQL ``update_state`` advances the row revision on
        every write, so each queued snapshot is aligned to the current row
        revision before it is applied (read-fresh + set ``revision`` = row+1).
        A concurrent drain that already applied a snapshot is skipped.
        """
        states: list[BatchState] = []
        while True:
            try:
                states.append(persist_queue.get_nowait())
            except queue.Empty:
                break
        lease = self._batch_lease(batch_id)
        for queued in states:
            try:
                result = await self._repos.batches.get(batch_id)
                if result is None:
                    continue
                _batch, current = result
                queued.revision = current.revision + 1
                await self._repos.batches.update_state(
                    batch_id,
                    state=queued,
                    expected_revision=current.revision,
                    lease_owner=lease[0] if lease else None,
                    lease_epoch=lease[1] if lease else None,
                    lease_duration_s=self._config.recovery_lease_seconds,
                )
            except StaleRevisionError:
                continue  # a concurrent drain already applied this snapshot

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

    async def _set_wire_with_versions(
        self, script_set: ScriptSet, items: list[ScriptItem]
    ) -> dict[str, Any]:
        """Fetch per-item ``current_version`` rows + latest gate for the wire."""
        versions_by_id: dict[str, ScriptVersion] = {}
        gate_by_item: dict[str, dict[str, Any] | None] = {}
        for item in items:
            if item.current_version_id is not None:
                v = await self._repos.versions.get(item.current_version_id)
                if v is not None:
                    versions_by_id[v.id] = v
            # Gate: latest gate run for the item (if any) — surface as _gate_wire shape.
            # Use the explicit gate_run lookup when a version has a run, else
            # fall back to the item's latest run.
            run = None
            if item.current_version_id is not None:
                run = await self._repos.gate_runs.latest_for_version(item.current_version_id)
            if run is None:
                runs = await self._repos.gate_runs.list_by_item(item.id)
                run = runs[-1] if runs else None
            if run is not None:
                # Same shape as ``_gate_wire(GateRunResult)`` so consumers read
                # one stable gate contract from both submit responses and reads.
                gate_by_item[item.id] = {
                    "state": "passed" if run.passed else "gate_failed",
                    "violations": [
                        {"rule_id": v.rule_id, "severity": v.severity, "message": v.message}
                        for v in run.violations
                    ],
                }
            else:
                gate_by_item[item.id] = None
        return self._set_wire(
            script_set, items, versions_by_id=versions_by_id, gate_by_item=gate_by_item
        )

    async def get_script_set(self, *, set_id: str) -> dict[str, Any] | None:
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            return None
        items = await self._repos.items.list_by_set(set_id)
        return await self._set_wire_with_versions(script_set, items)

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

    # ── AI generation / batch (B6) ───────────────────────────────────

    async def start_generation(
        self,
        *,
        set_id: str,
        product_id: str,
        target_duration_s: int,
        intent: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._require_accepting()
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        op_intent = ScriptIntent.GENERATE_LONG_FORM
        existing = await self._repos.jobs.find_by_idempotency(
            item.id, op_intent.value, idempotency_key
        )
        if existing is not None:
            return {"workflow_id": existing.id, "idempotent": True}
        llm_fn = self._require_llm()
        self._calibration().segment_count_for(target_duration_s)  # bounds check
        job, _lease_epoch = await self._create_job(
            item, set_id, product_id, op_intent, target_duration_s, idempotency_key
        )
        self._spawn(
            self._run_generation_job(job, item, script_set, llm_fn), name=f"sa-gen:{job.id}"
        )
        return {"workflow_id": job.id, "product_id": product_id, "status": "queued"}

    async def regenerate_segment(
        self,
        *,
        set_id: str,
        product_id: str,
        segment_index: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._require_accepting()
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        if item.state not in (ScriptState.GATE_FAILED, ScriptState.DRAFT):
            raise ScriptAuthoringError(
                "illegal_transition",
                f"cannot regenerate segment {segment_index} in state {item.state.name}",
            )
        llm_fn = self._require_llm()
        op_intent = ScriptIntent.REGENERATE_SEGMENT
        existing = await self._repos.jobs.find_by_idempotency(
            item.id, op_intent.value, idempotency_key
        )
        if existing is not None:
            return {
                "workflow_id": existing.id,
                "product_id": product_id,
                "segment_index": segment_index,
                "idempotent": True,
            }
        plan = await self._repos.plans.get_latest(item.id)
        target = plan.target_duration_s if plan is not None else self._config.min_target_duration_s
        job, _lease_epoch = await self._create_job(
            item, set_id, product_id, op_intent, target, idempotency_key
        )
        self._spawn(
            self._run_regenerate_job(job, item, script_set, segment_index, llm_fn),
            name=f"sa-regen:{job.id}",
        )
        return {
            "workflow_id": job.id,
            "product_id": product_id,
            "segment_index": segment_index,
            "status": "queued",
        }

    async def fix_with_ai(
        self,
        *,
        set_id: str,
        product_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._require_accepting()
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        item = await self._repos.items.get_by_product(set_id, product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        if item.current_version_id is None:
            raise ScriptAuthoringError("fix_not_eligible", "AI fix requires a gate-failed version")
        current_version = await self._repos.versions.get(item.current_version_id)
        if current_version is None:
            raise ScriptAuthoringError("fix_not_eligible", "current draft version is missing")
        workflow = self._make_workflow(
            item, current_version, _SyncPersistBridge(), script_set.brief.transition_policy
        )
        try:
            workflow.fix_eligible()
        except InvalidFixStateError as exc:
            raise ScriptAuthoringError("fix_not_eligible", str(exc)) from exc
        llm_fn = self._require_llm()
        op_intent = ScriptIntent.FIX_FAILED
        existing = await self._repos.jobs.find_by_idempotency(
            item.id, op_intent.value, idempotency_key
        )
        if existing is not None:
            return {"workflow_id": existing.id, "product_id": product_id, "idempotent": True}
        job, _lease_epoch = await self._create_job(
            item,
            set_id,
            product_id,
            op_intent,
            self._config.min_target_duration_s,
            idempotency_key,
        )
        self._spawn(
            self._run_fix_job(job, item, script_set, current_version, llm_fn),
            name=f"sa-fix:{job.id}",
        )
        return {"workflow_id": job.id, "product_id": product_id, "status": "queued"}

    async def start_batch_generation(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        target_duration_s: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        self._require_accepting()
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        items: dict[str, ScriptItem] = {}
        for pid in product_ids:
            item = await self._repos.items.get_by_product(set_id, pid)
            if item is None:
                self._raise_not_found("product script", pid)
            items[pid] = item
        existing_batch = await self._repos.batches.find_by_idempotency(set_id, idempotency_key)
        if existing_batch is not None:
            return {
                "batch_id": existing_batch.id,
                "workflow_summary": {"products": [], "estimated_semantic_calls_total": 0},
                "status": existing_batch.status.value,
                "idempotent": True,
            }
        llm_fn = self._require_llm()
        self._calibration().segment_count_for(target_duration_s)  # bounds check
        req = BatchRequest(
            script_set_id=set_id,
            script_set_revision=script_set.revision,
            requested_products=tuple(product_ids),
            target_durations=tuple((pid, float(target_duration_s)) for pid in product_ids),
            max_product_concurrency=self._config.max_concurrent_products,
            max_attempts=self._config.provider_max_attempts,
            model_fingerprint=self._model_fingerprint(),
            client_key=idempotency_key,
        )
        fingerprint = request_fingerprint(req)
        existing_id = await self._repos.idempotency.get(fingerprint)
        if existing_id is not None:
            existing = await self._repos.batches.get(existing_id)
            if existing is not None:
                batch, state = existing
                return {
                    "batch_id": batch.id,
                    "workflow_summary": {"products": [], "estimated_semantic_calls_total": 0},
                    "status": state.status,
                    "idempotent": True,
                }
        # Bind the per-batch runtime structures BEFORE starting the orchestrator
        # so its persist / event sinks and the driver factory close over them.
        # The orchestrator generates its own batch id during start(); the ring /
        # event sink resolve it lazily so the SSE ring is keyed by the same id
        # that is persisted in the batch row.
        bridge = _SyncPersistBridge()
        persist_queue: queue.Queue[BatchState] = queue.Queue()
        batch_holder: dict[str, str] = {}

        def _batch_id() -> str:
            return batch_holder.get("batch_id", "")

        def event_sink(event: str, payload: dict) -> None:
            bid = _batch_id()
            self._batch_sink(bid, self._event_ring(bid), event, payload)

        def create_workflow(product_id: str, target: float):
            item = items[product_id]
            return self._build_driver(
                item,
                script_set,
                int(target),
                llm_fn,
                bridge,
                emit=event_sink,
                batch_id=_batch_id,
                loaders=self._make_loaders(items, {}, {}),
            )

        orch = BatchScriptGenerationOrchestrator(
            create_workflow,
            config=BatchOrchestratorConfig(
                max_product_concurrency=self._config.max_concurrent_products,
                max_attempts=self._config.provider_max_attempts,
                model_fingerprint=self._model_fingerprint(),
            ),
            event_sink=event_sink,
            persist=lambda state: persist_queue.put(state.model_copy(deep=True)),
            idempotency=DbIdempotencyRegistry(self._repos.idempotency),
        )
        state, created = orch.start(req)
        batch_holder["batch_id"] = state.batch_id
        self._event_ring(state.batch_id)  # ensure the ring exists for SSE
        batch = GenerationBatch(
            id=state.batch_id,
            script_set_id=set_id,
            status=GenerationBatchStatus.QUEUED,
            product_ids=list(state.requested_products),
            job_ids=[],
            estimated_semantic_calls=state.planned_semantic_calls,
            idempotency_key=idempotency_key,
        )
        async with self._repos.transaction() as conn:
            await self._repos.batches.insert(batch, state=state, conn=conn)
            lease_epoch = await self._repos.batches.acquire_lease(
                batch.id, self._instance_id, self._config.recovery_lease_seconds, conn=conn
            )
        batch.lease_owner = self._instance_id
        batch.lease_epoch = lease_epoch
        self._leased_batches[batch.id] = lease_epoch
        await self._repos.idempotency.register(fingerprint, state.batch_id)
        self._active_orchestrators[state.batch_id] = orch
        self._batch_persist_queues[state.batch_id] = persist_queue
        self._batch_artifact_bridges[state.batch_id] = bridge
        revisions = {item.id: item.revision for item in items.values()}
        self._spawn(
            self._run_batch_job(state.batch_id, orch, persist_queue, bridge, revisions, set()),
            name=f"sa-batch:{state.batch_id}",
        )
        return {
            "batch_id": state.batch_id,
            "workflow_summary": state.preview,
            "status": "queued",
        }

    async def get_batch(self, *, set_id: str, batch_id: str) -> dict[str, Any] | None:
        result = await self._repos.batches.get(batch_id)
        if result is None:
            self._raise_not_found("generation batch", batch_id)
        _batch, state = result
        return {
            "batch_id": batch_id,
            "status": state.status,
            "product_ids": list(state.requested_products),
        }

    async def cancel_batch(self, *, set_id: str, batch_id: str) -> dict[str, Any] | None:
        result = await self._repos.batches.get(batch_id)
        if result is None:
            self._raise_not_found("generation batch", batch_id)
        _batch, state = result
        if state.status not in ("running", "queued"):
            raise ScriptAuthoringError(
                "illegal_transition", f"cannot cancel batch in state {state.status}"
            )
        # R8.4: the durable cancel request is the single source of truth and is
        # persisted by ANY replica (idempotent). A non-owner NEVER reconstructs
        # a runner, claims the lease, or writes progress/artifacts.
        await self._repos.batches.request_cancel(batch_id)
        is_owner = batch_id in self._leased_batches or batch_id in self._active_orchestrators
        if is_owner:
            # Low-latency in-memory signal (R8.4 allowed optimization, NOT a
            # second semantic path): the owner loop polls the durable request
            # and persists the terminal CANCELLED under its own fence.
            orch = self._active_orchestrators.get(batch_id)
            if orch is not None:
                try:
                    orch.cancel()
                except Exception:
                    pass
            # The cancelled snapshot (and any queued step snapshots) are
            # persisted by _drain_batch_persists under the owner's fence.
            persist_queue = self._batch_persist_queues.get(batch_id)
            if persist_queue is not None:
                try:
                    await self._drain_batch_persists(batch_id, persist_queue)
                except Exception:
                    pass
            # Drain any enqueued artifact writes so the item rows reflect cancels.
            bridge = self._batch_artifact_bridges.get(batch_id)
            if bridge is not None:
                try:
                    revisions = await self._current_item_revisions(batch_id)
                    await self._drain_artifacts(
                        bridge,
                        revisions,
                        set(),
                        batch_id=batch_id,
                        batch_lease=self._batch_lease(batch_id),
                    )
                except Exception:
                    pass
        return {"batch_id": batch_id, "status": "cancelling"}

    async def get_batch_events_snapshot(self, *, set_id: str, batch_id: str) -> str | None:
        result = await self._repos.batches.get(batch_id)
        if result is None:
            return None
        _batch, state = result
        return json.dumps(
            {
                "batch_id": batch_id,
                "set_id": set_id,
                "status": state.status,
                "revision": state.revision,
            }
        )

    async def stream_batch_events(
        self, *, set_id: str, batch_id: str
    ) -> AsyncIterator[dict[str, str]]:
        if await self._repos.batches.get(batch_id) is None:
            return
        ring = self._event_ring(batch_id)
        consumed = 0
        while True:
            while consumed < len(ring):
                event = ring[consumed]
                consumed += 1
                yield event
            if any(
                e["event"] in ("batch.completed", "batch.cancelled", "batch.error") for e in ring
            ):
                return
            if await self._repos.batches.get(batch_id) is None:
                yield {
                    "event": "batch.error",
                    "data": json.dumps(
                        {"set_id": set_id, "batch_id": batch_id, "code": "not_found"}
                    ),
                }
                return
            await asyncio.sleep(0.05)

    # ── B6 background jobs ────────────────────────────────────────────

    async def recover_pending(self) -> None:
        """Resume durable jobs/batches left mid-flight by a previous process.

        HIGH-B / R6.3-R6.4: called from the lifespan startup AFTER the
        authoring repositories connect. Reconstructs each recoverable workflow
        from its persisted finite state and ``_spawn``s execution so the job
        actually resumes and reaches a durable terminal status — merely
        returning the existing workflow id (idempotency) is NOT recovery.

        Duplicate-runner safety (two layers):
        1. cross-process — ``claim_recoverable`` atomically fences each
           running/queued row to exactly ONE owner with a PostgreSQL lease
           (owner + expiry + fencing epoch); a concurrent replica's claim on
           the same row matches zero rows;
        2. in-process — ``_owns_task`` skips a job/batch this service already
           drives an active task for, so a repeated ``recover_pending`` in the
           same process never double-spawns.
        """
        if not self._accepting_jobs:
            return
        lease_s = self._config.recovery_lease_seconds
        for job in await self._repos.jobs.claim_recoverable(self._instance_id, lease_s):
            name = f"sa-recover:{job.id}"
            if self._owns_task(f"sa-gen:{job.id}") or self._owns_task(name):
                continue
            self._leased_jobs[job.id] = job.lease_epoch
            self._spawn(self._resume_job(job), name=name)
        for batch, _state in await self._repos.batches.claim_recoverable(
            self._instance_id, lease_s
        ):
            name = f"sa-recover-batch:{batch.id}"
            if self._owns_task(f"sa-batch:{batch.id}") or self._owns_task(name):
                continue
            self._leased_batches[batch.id] = batch.lease_epoch
            self._spawn(self._resume_batch(batch.id), name=name)

    async def _resume_job(self, job: GenerationJob) -> None:
        """Dispatch recovery for one durable job by its intent (R6.3)."""
        if job.intent != ScriptIntent.GENERATE_LONG_FORM:
            # regenerate/fix jobs cannot be safely resumed without their exact
            # input parameters (target segment index / failed-rule context),
            # which are not persisted on the job row; mark them deterministically
            # FAILED so no job is left RUNNING forever. The item stays in its
            # durable state (e.g. GATE_FAILED), so a fresh regenerate/fix can be
            # issued.
            await self._fail_job(job, f"job intent {job.intent.value} is not resumable")
            return
        await self._resume_generation_job(job)

    async def _fail_job(self, job: GenerationJob, message: str) -> None:
        """Land a durable job FAILED so it is not left RUNNING forever."""
        job.status = GenerationJobStatus.FAILED
        try:
            await self._repos.jobs.update(
                job,
                lease_owner=job.lease_owner,
                lease_epoch=job.lease_epoch,
                lease_duration_s=self._config.recovery_lease_seconds,
            )
        except Exception:
            pass

    async def _resume_generation_job(self, job: GenerationJob) -> None:
        """Reconstruct + drive a durable RUNNING generation job to terminal.

        Reuses the persisted plan (never re-plans), regenerates only uncommitted
        segments, and does not duplicate committed immutable artifacts.
        """
        item = await self._repos.items.get(job.script_item_id)
        if item is None:
            await self._fail_job(job, "script item missing at recovery")
            return
        script_set = await self._repos.script_sets.get(item.script_set_id)
        if script_set is None:
            await self._fail_job(job, "script set missing at recovery")
            return
        llm_fn = self._safe_llm()
        if llm_fn is None:
            await self._fail_job(job, "LLM unavailable at recovery")
            return
        try:
            driver, bridge = await self._reconstruct_generation_driver(
                job, item, script_set, llm_fn
            )
        except Exception as exc:
            await self._fail_job(job, f"recovery reconstruction failed: {exc}")
            return
        revisions = {item.id: item.revision}
        existing_versions: set[str] = set()
        await self._drive_generation(job, driver, bridge, revisions, existing_versions)
        # A reconstructed workflow that could not advance to a terminal state
        # (e.g. the item was left in a non-generation state such as DRAFT, so
        # ``driver.step`` returns False without a terminal status) must not stay
        # RUNNING forever — land it deterministically FAILED.
        if job.status is GenerationJobStatus.RUNNING:
            await self._fail_job(job, "recovered job did not advance to a terminal state")

    async def _reconstruct_generation_driver(self, job, item, script_set, llm_fn):
        """Rebuild a ``WorkflowDriver`` for a durable job from persisted rows.

        Replays only the finite counters (plan id / segment count / persisted
        segment + version rows) so the restored driver resumes at the next
        unresolved segment instead of re-planning or regenerating committed
        segments. The reconstructed snapshot mirrors ``WorkflowDriver.snapshot()``
        so ``driver.restore`` replays exactly the deterministic finite state.
        """
        plan = None
        if job.plan_id:
            plan = await self._repos.plans.get(job.plan_id)
        if plan is None:
            plan = await self._repos.plans.get_latest(item.id)
        segments: dict[str, ScriptSegment] = {}
        segment_versions: list[dict] = []
        plan_segment_count = job.plan_segment_count
        if plan is not None:
            plan_segment_count = plan.segment_count
            for seg in await self._repos.segments.list_by_plan(plan.id):
                segments[seg.id] = seg
                segment_versions.append(
                    {"index": seg.segment_index, "id": seg.id, "status": seg.status.value}
                )
        versions: dict[str, ScriptVersion] = {}
        version_ids: list[str] = []
        for ver in await self._repos.versions.list_by_item(item.id):
            versions[ver.id] = ver
            version_ids.append(ver.id)
        bridge = _SyncPersistBridge()
        driver = self._build_driver(
            item,
            script_set,
            job.target_duration_s,
            llm_fn,
            bridge,
            emit=lambda *a, **k: None,
            batch_id="",
            loaders=self._make_loaders({item.id: item}, segments, versions),
        )
        snapshot = {
            "product_id": item.product_id,
            "item_id": item.id,
            "state": item.state.value,
            "current_version_id": item.current_version_id,
            "approved_version_id": item.approved_version_id,
            "plan_id": plan.id if plan is not None else job.plan_id,
            "plan_segment_count": plan_segment_count,
            "target_duration_s": job.target_duration_s,
            "semantic_calls": 0,
            "segment_versions": segment_versions,
            "version_ids": version_ids,
        }
        driver.restore(snapshot)
        return driver, bridge

    async def _drive_generation(
        self,
        job: GenerationJob,
        driver,
        bridge: _SyncPersistBridge,
        revisions: dict[str, int],
        existing_versions: set[str],
    ) -> None:
        """Drive a generation driver to terminal with the shared drain loop."""
        try:
            # Each finite step runs the (sync) provider call off the loop
            # (HIGH-2) so a slow LLM never stalls health/SSE/cancellation,
            # and renews the job lease while the call is in flight (R8.3) so
            # a healthy slow call is not falsely taken over.
            while True:
                done = await self._with_lease_heartbeat(driver.step, job=job)
                if not done:
                    break
                await self._drain_artifacts(bridge, revisions, existing_versions, job=job)
                await self._update_job(
                    job, driver.workflow, self._job_status_for(driver.workflow.item.state)
                )
                await asyncio.sleep(0)
            await self._drain_artifacts(bridge, revisions, existing_versions, job=job)
            await self._update_job(
                job, driver.workflow, self._job_status_for(driver.workflow.item.state)
            )
        except LeaseLostError:
            # Another replica claimed this job's lease; stop writing entirely.
            # The row is now owned by that replica, which will drive it to a
            # terminal state — we must not land it FAILED here.
            return
        except Exception as exc:
            # Deterministic failure: land the item FAILED and record the job.
            try:
                self._land_failed(driver.workflow, str(exc) or type(exc).__name__, bridge)
                await self._drain_artifacts(bridge, revisions, existing_versions, job=job)
            except Exception:
                pass
            try:
                await self._update_job(job, driver.workflow, GenerationJobStatus.FAILED)
            except Exception:
                pass

    async def _resume_batch(self, batch_id: str) -> None:
        """Resume a durable QUEUED/RUNNING batch after a restart (R6.4)."""
        result = await self._repos.batches.get(batch_id)
        if result is None:
            return
        _batch, _state = result
        # The batch was atomically claimed in ``recover_pending``; carry the
        # acquired fence onto the drive loop so progress writes stay guarded.
        if _batch.lease_owner == self._instance_id:
            self._leased_batches[batch_id] = _batch.lease_epoch
        # R8.4 crash-after-cancel: a durable cancel request wins over resuming
        # semantic work. Land the terminal CANCELLED under the claimed fence
        # WITHOUT scheduling any new provider calls.
        if _batch.cancel_requested:
            try:
                terminal = _state.model_copy(deep=True)
                terminal.status = "cancelled"
                terminal.bump_revision()
                await self._repos.batches.update_state(
                    batch_id,
                    state=terminal,
                    expected_revision=_state.revision,
                    lease_owner=self._instance_id,
                    lease_epoch=_batch.lease_epoch,
                    lease_duration_s=self._config.recovery_lease_seconds,
                )
            except (LeaseLostError, StaleRevisionError):
                pass  # fence lost / concurrently terminalized — nothing to write
            return
        try:
            (
                orch,
                persist_queue,
                bridge,
                revisions,
                existing_versions,
            ) = await self._recover_batch_runner(batch_id)
        except Exception:
            return  # already terminal / not recoverable; nothing to resume
        if orch is None:
            return
        await self._run_batch_job(
            batch_id, orch, persist_queue, bridge, revisions, existing_versions
        )

    async def _recover_batch_runner(self, batch_id: str):
        """Rebuild the batch runner from persisted state + seed drain guards."""
        result = await self._repos.batches.get(batch_id)
        if result is None:
            return None, None, None, {}, set()
        _batch, state = result
        orch, persist_queue, bridge = await self._recover_orchestrator(
            batch_id, state.script_set_id
        )
        if orch is None:
            return None, None, None, {}, set()
        revisions: dict[str, int] = {}
        existing_versions: set[str] = set()
        for pid in state.requested_products:
            item = await self._repos.items.get_by_product(state.script_set_id, pid)
            if item is not None:
                revisions[item.id] = item.revision
                for ver in await self._repos.versions.list_by_item(item.id):
                    existing_versions.add(ver.id)
        return orch, persist_queue, bridge, revisions, existing_versions

    async def drain(self, *, timeout_s: float | None = None) -> None:
        """Quiesce owned background jobs before the repository pool closes.

        HIGH-1: the lifespan runs this BEFORE ``close_authoring``. Semantics:
        1. stop admitting new work — the AI commands now raise
           ``service_unavailable``;
        2. give owned tasks a bounded (wall-clock) window to complete
           gracefully — each successful job persists a durable terminal row;
        3. cancel stragglers and await them so no task can touch the pool after
           ``close()`` — an unfinished durable job stays RUNNING and is
           recoverable by the next worker from its persisted snapshot;
        4. clear the registry.

        A task blocked inside a long sync provider call finishes that call
        first (cancellation is only delivered at the next await); moving those
        calls to ``asyncio.to_thread`` (HIGH-2) makes this return promptly even
        mid-call.
        """
        self._accepting_jobs = False
        tasks = [t for t in self._tasks if not t.done()]
        if tasks:
            timeout = timeout_s if timeout_s is not None else self._config.drain_timeout_s
            _, pending = await asyncio.wait(tasks, timeout=timeout)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        await self._release_held_leases()

    async def _release_held_leases(self) -> None:
        """Best-effort release every lease this process still holds.

        Runs on graceful shutdown so a healthy restart can recover the rows
        immediately instead of waiting out ``recovery_lease_seconds``. Must
        never raise — drain is a shutdown path.
        """
        for job_id in list(self._leased_jobs):
            try:
                await self._repos.jobs.release_lease(job_id, self._instance_id)
            except Exception:
                pass
        for batch_id in list(self._leased_batches):
            try:
                await self._repos.batches.release_lease(batch_id, self._instance_id)
            except Exception:
                pass
        self._leased_jobs.clear()
        self._leased_batches.clear()

    async def _run_generation_job(
        self,
        job: GenerationJob,
        item: ScriptItem,
        script_set: ScriptSet,
        llm_fn,
        driver=None,
    ) -> None:
        """Drive a generation job to terminal.

        ``driver`` may be a pre-built/restored driver (recovery path) or None,
        in which case a fresh driver is built from scratch (new-job path). The
        shared ``_drive_generation`` loop drains artifacts + updates the job row
        after every finite step.
        """
        bridge = _SyncPersistBridge()
        if driver is None:
            driver = self._build_driver(
                item,
                script_set,
                job.target_duration_s,
                llm_fn,
                bridge,
                emit=lambda *a, **k: None,
                batch_id="",
                loaders=self._make_loaders({item.id: item}, {}, {}),
            )
        revisions = {item.id: item.revision}
        existing_versions: set[str] = set()
        await self._drive_generation(job, driver, bridge, revisions, existing_versions)

    async def _run_regenerate_job(
        self, job, item: ScriptItem, script_set: ScriptSet, segment_index: int, llm_fn
    ) -> None:
        bridge = _SyncPersistBridge()
        revisions = {item.id: item.revision}
        existing_versions: set[str] = set()
        try:
            plan = await self._repos.plans.get_latest(item.id)
            if plan is None:
                raise ValueError("no plan to regenerate a segment from")
            segments = await self._repos.segments.list_by_plan(plan.id)
            if segment_index < 0 or segment_index >= len(segments):
                raise ValueError(f"segment index {segment_index} out of range")
            current_segment = segments[segment_index]
            parts = build_generate_prompt(
                self._skill_text(),
                generation_constraints=[],
                context=AuthoritativeContext(),
                duration_s=plan.target_duration_s,
                intent=GenScriptIntent(intent="selling", target_duration_s=plan.target_duration_s),
                transition=build_transition_context(script_set.brief.transition_policy),
                plan={"product_id": item.product_id, "plan_id": plan.id},
                segment_index=segment_index,
            )
            prompt = "\n\n".join(filter(None, [parts.system, parts.context, parts.user]))
            # HIGH-2 keeps the loop free; the lease heartbeat (R8.3) keeps the
            # fence alive while this slow provider call is in flight.
            raw = await self._with_lease_heartbeat(lambda: llm_fn(prompt), job=job)
            if not raw or not raw.strip():
                raise ValueError("regenerated segment text is empty")
            new_segment = ScriptSegment(
                id=new_id("segment"),
                script_item_id=item.id,
                plan_id=plan.id,
                segment_index=segment_index,
                title=current_segment.title,
                intent=current_segment.intent,
                target_duration_s=current_segment.target_duration_s,
                display_text=raw,
                spoken_text=raw,
                status=ScriptState.DRAFT,
                version=current_segment.version + 1,
            )
            ordered = list(segments)
            ordered[segment_index] = new_segment
            context = ScriptGateContext(
                transition_policy=script_set.brief.transition_policy, facts=ProductFacts()
            )
            result = self._gate.run_full_script([s.spoken_text for s in ordered], context)
            run = GateRun(
                id=new_id("gate_run"),
                script_item_id=item.id,
                full=True,
                passed=result.passed,
                violations=[_to_gate_violation(v) for v in result.violations],
                rule_set_fingerprint=result.fingerprint.hexdigest,
                script_version_id=None,
            )
            versions = await self._repos.versions.list_by_item(item.id)
            version = ScriptVersion(
                id=new_id("script_version"),
                script_item_id=item.id,
                version=len(versions) + 1,
                state=ScriptState.REVIEWABLE if result.passed else ScriptState.GATE_FAILED,
                source=ScriptSource.AI_REGENERATE,
                display_text="\n\n".join(s.display_text for s in ordered),
                spoken_text="\n\n".join(s.spoken_text for s in ordered),
                segment_version_ids=[s.id for s in ordered],
                gate_run_id=run.id,
            )
            item.state = ScriptState.REVIEWABLE if result.passed else ScriptState.GATE_FAILED
            item.current_version_id = version.id
            bridge(new_segment)
            bridge(run)
            bridge(version)
            bridge(item)
            await self._drain_artifacts(bridge, revisions, existing_versions, job=job)
            job.status = (
                GenerationJobStatus.COMPLETED if result.passed else GenerationJobStatus.FAILED
            )
            job.plan_id = plan.id
            job.plan_segment_count = len(segments)
            job.current_segment_index = segment_index
            await self._repos.jobs.update(
                job,
                lease_owner=job.lease_owner,
                lease_epoch=job.lease_epoch,
                lease_duration_s=self._config.recovery_lease_seconds,
            )
        except LeaseLostError:
            # R8.3: another replica owns the fence now — discard the result,
            # commit no artifacts, and do NOT mark the durable job FAILED.
            return
        except Exception as exc:
            try:
                self._land_failed(_RegenWorkflow(item), str(exc) or type(exc).__name__, bridge)
                await self._drain_artifacts(bridge, revisions, existing_versions, job=job)
            except Exception:
                pass
            try:
                await self._repos.jobs.update(
                    job,
                    lease_owner=job.lease_owner,
                    lease_epoch=job.lease_epoch,
                    lease_duration_s=self._config.recovery_lease_seconds,
                )
            except Exception:
                pass

    async def _run_fix_job(
        self, job, item: ScriptItem, script_set: ScriptSet, current_version, llm_fn
    ) -> None:
        bridge = _SyncPersistBridge()
        workflow = self._make_workflow(
            item, current_version, bridge, script_set.brief.transition_policy
        )
        workflow.generate = self._make_fix_generate(
            current_version, llm_fn, await self._failed_rule_ids(item.id)
        )
        try:
            try:
                # apply_ai_fix runs the (sync) repair provider call; off the
                # loop (HIGH-2) + lease heartbeat (R8.3) so a slow fix neither
                # stalls the loop nor lets the fence lapse.
                await self._with_lease_heartbeat(workflow.apply_ai_fix, job=job)
            except LeaseLostError:
                return  # R8.3: another replica owns the fence; no FAILED
            except Exception:
                pass  # apply_ai_fix persists the item FAILED on provider failure
            await self._persist_workflow(
                workflow,
                bridge,
                item_revision=item.revision,
                existing_version_ids={current_version.id},
                job=job,
            )
            job.status = (
                GenerationJobStatus.COMPLETED
                if item.state is ScriptState.DRAFT
                else GenerationJobStatus.FAILED
            )
            await self._repos.jobs.update(
                job,
                lease_owner=job.lease_owner,
                lease_epoch=job.lease_epoch,
                lease_duration_s=self._config.recovery_lease_seconds,
            )
        except LeaseLostError:
            return  # fence lost before the final persist; stop writing

    def _make_fix_generate(self, current_version, llm_fn, failed_rules):
        failed_ids = [rule_id for rule_id, _message in failed_rules]
        instructions = [message for _rule_id, message in failed_rules]
        if not failed_ids:
            failed_ids = ["REVIEW"]
            instructions = ["Review the script and fix any remaining issues."]
        parts = build_repair_prompt(
            source_text=current_version.spoken_text,
            failed_rule_ids=failed_ids,
            rule_repair_instructions=instructions,
            authoritative_facts=AuthoritativeContext(),
        )
        prompt = "\n\n".join(filter(None, [parts.system, parts.context, parts.user]))

        def generate():
            raw = llm_fn(prompt)
            return SegmentGenerationResult(segment_index=0, display_text=raw, spoken_text=raw)

        return generate

    async def _failed_rule_ids(self, item_id: str) -> list[tuple[str, str]]:
        runs = await self._repos.gate_runs.list_by_item(item_id)
        if not runs:
            return []
        last = runs[-1]
        return [(violation.rule_id, violation.message) for violation in last.violations]

    async def _run_batch_job(
        self,
        batch_id: str,
        orch: BatchScriptGenerationOrchestrator,
        persist_queue: queue.Queue[BatchState],
        bridge: _SyncPersistBridge,
        revisions: dict[str, int],
        existing_versions: set[str],
    ) -> None:
        try:
            while True:
                lease = self._batch_lease(batch_id)
                # One scheduler round runs the (sync) provider calls off the
                # loop (HIGH-2) so a slow LLM never stalls health/SSE/cancel,
                # and renews the batch fence while in flight (R8.3) so a
                # healthy slow round is not falsely taken over.
                state = await self._with_lease_heartbeat(
                    orch.step,
                    batch_id=batch_id,
                    batch_lease=lease if lease else None,
                )
                await self._drain_artifacts(
                    bridge,
                    revisions,
                    existing_versions,
                    batch_id=batch_id,
                    batch_lease=lease if lease else None,
                )
                await self._drain_batch_persists(batch_id, persist_queue)
                if state.status in ("completed", "partial_completed", "failed", "cancelled"):
                    break
                # R8.4: honor a durable cross-replica cancel request before
                # scheduling the next semantic round. Stop, terminalize the
                # batch CANCELLED under the owner+epoch fence, and exit.
                if await self._batch_cancel_requested(batch_id):
                    try:
                        orch.cancel()
                    except Exception:
                        pass
                    await self._drain_artifacts(
                        bridge,
                        revisions,
                        existing_versions,
                        batch_id=batch_id,
                        batch_lease=lease if lease else None,
                    )
                    await self._drain_batch_persists(batch_id, persist_queue)
                    break
                await asyncio.sleep(0.02)
        except LeaseLostError:
            return  # another replica claimed the batch lease; stop writing
        except Exception:
            pass  # failures are already persisted into per-product BatchState rows

    async def _batch_cancel_requested(self, batch_id: str) -> bool:
        """Return True when a durable cross-replica cancel request is pending.

        Reads the batch row (the source of truth), so a request persisted by
        any replica is visible to the owner loop on its next poll.
        """
        result = await self._repos.batches.get(batch_id)
        return result is not None and result[0].cancel_requested

    async def _current_item_revisions(self, batch_id: str) -> dict[str, int]:
        result = await self._repos.batches.get(batch_id)
        if result is None:
            return {}
        _batch, state = result
        revisions: dict[str, int] = {}
        for pid in state.requested_products:
            item = await self._repos.items.get_by_product(state.script_set_id, pid)
            if item is not None:
                revisions[item.id] = item.revision
        return revisions

    async def _recover_orchestrator(self, batch_id: str, set_id: str):
        """Rebuild a cancelled batch from persisted state (task 10.8).

        Pre-loads the item / segment / version rows referenced by each
        workflow snapshot so the driver's sync loaders are pure dict lookups
        (no sync-over-async from the event-loop thread).
        """
        result = await self._repos.batches.get(batch_id)
        if result is None:
            return None, None, None
        _batch, state = result
        script_set = await self._repos.script_sets.get(set_id)
        if script_set is None:
            return None, None, None
        items: dict[str, ScriptItem] = {}
        for pid in state.requested_products:
            item = await self._repos.items.get_by_product(set_id, pid)
            if item is not None:
                items[pid] = item
        segments: dict[str, ScriptSegment] = {}
        versions: dict[str, ScriptVersion] = {}
        for product_state in state.products.values():
            snap = product_state.workflow_snapshot or {}
            for entry in snap.get("segment_versions", []):
                seg = await self._repos.segments.get(entry["id"])
                if seg is not None:
                    segments[seg.id] = seg
            for version_id in snap.get("version_ids", []):
                ver = await self._repos.versions.get(version_id)
                if ver is not None:
                    versions[ver.id] = ver
        llm_fn = self._safe_llm()
        bridge = _SyncPersistBridge()
        persist_queue: queue.Queue[BatchState] = queue.Queue()
        ring = self._event_ring(batch_id)

        def create_workflow(product_id: str, target: float):
            item = items[product_id]
            return self._build_driver(
                item,
                script_set,
                int(target),
                llm_fn,
                bridge,
                emit=lambda event, payload: self._batch_sink(batch_id, ring, event, payload),
                batch_id=batch_id,
                loaders=self._make_loaders(items, segments, versions),
            )

        orch = BatchScriptGenerationOrchestrator(
            create_workflow,
            config=BatchOrchestratorConfig(
                max_product_concurrency=self._config.max_concurrent_products,
                max_attempts=self._config.provider_max_attempts,
                model_fingerprint=self._model_fingerprint(),
            ),
            event_sink=lambda event, payload: self._batch_sink(batch_id, ring, event, payload),
            persist=lambda s: persist_queue.put(s.model_copy(deep=True)),
            idempotency=DbIdempotencyRegistry(self._repos.idempotency),
        )
        recover_batch(orch, state)
        self._active_orchestrators[batch_id] = orch
        self._batch_persist_queues[batch_id] = persist_queue
        self._batch_artifact_bridges[batch_id] = bridge
        return orch, persist_queue, bridge
