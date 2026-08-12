"""Multi-product bounded batch generation (tasks 10.1-10.8).

``BatchScriptGenerationOrchestrator`` creates exactly ONE finite
``ProductGenerationWorkflow`` per selected product (injected via
``create_workflow``) — never one multi-product LLM response (Decision 10).
A ``BoundedScheduler`` keeps at most ``max_product_concurrency`` workflows
in semantic work; the rest stay queued.

Design properties this module owns (Decisions 12/20):

- transport failures only are retried, up to ``max_attempts``, against the
  same immutable job input; a transport retry does NOT increment the
  semantic job counter (only a completed job does). Content/gate failures
  NEVER retry.
- idempotent submission: an equivalent request (same product set,
  durations, config, fingerprint, client key) returns the existing running
  batch instead of double-spending model calls.
- cancellation stops scheduling new semantic calls, preserves completed
  artifacts, persists cancelled states, and emits a terminal event.
- persisted ``BatchState`` lets ``recover_batch`` resume from the finite
  next step/segment index without reinterpreting model prose.

Scheduling is synchronous and caller-driven (``step``): tests step a fake
clock and inspect recorded active windows — no asyncio race dependence.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Optional, Protocol, TypeVar

from pydantic import BaseModel, Field

from backend.application.script_authoring.generation.scheduler import BoundedScheduler
from backend.application.script_authoring.models import new_id

TWorkflow = TypeVar("TWorkflow", bound="ProductGenerationWorkflow")


# ── errors ────────────────────────────────────────────────────────────────


class GenerationError(Exception):
    """Base for batch orchestration failures."""


class TransportError(GenerationError):
    """Typed provider/transport failure — the ONLY retryable error.

    Content failures (gate/malformed output) raise other exceptions and are
    never retried (Decision 12).
    """


class ContentFailure(GenerationError):
    """Content/gate failure of a workflow. Never automatically retried."""


class BatchCancelledError(GenerationError):
    """Raised when stepping a batch that was cancelled."""


# ── workflow protocol (injected; other clusters implement) ────────────────


class ProductGenerationWorkflow(Protocol):
    """Finite per-product workflow owned by cluster 9.

    The batch orchestrator only drives the protocol surface — it never
    interprets model prose. State is persisted by the workflow itself
    (``snapshot``/``restore``), so the batch can recover a workflow from a
    persisted step without re-running completed segments.
    """

    product_id: str

    def step(self) -> bool:
        """Advance one finite step (plan -> one segment -> compile -> gate).

        Returns False when the workflow reached a terminal state
        (completed, failed, or cancelled); True while more work remains.
        """
        ...

    def is_terminal(self) -> bool: ...

    def snapshot(self) -> dict:
        """Serializable finite state for persistence/recovery."""
        ...

    def restore(self, state: dict) -> None: ...


# ── persisted state (task 10.3) ───────────────────────────────────────────


class ProductWorkflowState(BaseModel):
    """Per-product persisted workflow state within a batch (task 10.3)."""

    model_config = {"extra": "forbid"}

    product_id: str = Field(min_length=1)
    status: str = "queued"  # queued|running|completed|failed|cancelled
    current_segment_index: int = Field(default=0, ge=0)
    plan_segment_count: int = Field(default=0, ge=0)
    transport_attempts: int = Field(default=0, ge=0)
    semantic_calls: int = Field(default=0, ge=0)
    error: str = Field(default="")
    workflow_snapshot: dict = Field(default_factory=dict)


class BatchState(BaseModel):
    """Full persisted batch state (task 10.3/10.8).

    Holds the requested product set, per-product target durations, the
    fixed call preview, current per-product workflow states, aggregate
    counts, and planned/actual semantic call counters. ``model_dump`` of
    this object is the persistence payload: ``recover_batch`` resumes from
    it without reinterpreting any model output.
    """

    model_config = {"extra": "forbid"}

    batch_id: str = Field(min_length=1)
    script_set_id: str = Field(min_length=1)
    status: str = "queued"  # queued|running|completed|partial_completed|cancelled|failed
    requested_products: list[str] = Field(default_factory=list)
    target_durations: dict[str, float] = Field(default_factory=dict)
    preview: dict = Field(default_factory=dict)
    products: dict[str, ProductWorkflowState] = Field(default_factory=dict)
    planned_semantic_calls: int = Field(default=0, ge=0)
    actual_semantic_calls: int = Field(default=0, ge=0)
    started: int = Field(default=0, ge=0)
    completed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    cancelled: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)

    def bump_revision(self) -> None:
        """Increment the monotonic revision after every mutation."""
        self.revision += 1


# ── events ────────────────────────────────────────────────────────────────

EventSink = Callable[[str, dict], None]


def _default_event_sink(event: str, payload: dict) -> None:
    """No-op default sink; the SSE layer (task 11.10) replaces this."""


# ── idempotency (task 10.6) ───────────────────────────────────────────────


@dataclass
class BatchRequest:
    """Immutable batch-generation request (idempotency identity input)."""

    script_set_id: str
    script_set_revision: int
    requested_products: tuple[str, ...]
    target_durations: tuple[tuple[str, float], ...]
    max_product_concurrency: int
    max_attempts: int
    model_fingerprint: str  # model/skill/rules fingerprint (Decision 12)
    client_key: str = ""  # client Idempotency-Key


def request_fingerprint(req: BatchRequest) -> str:
    """Stable sha256 over the full idempotency identity (Decision 12)."""
    parts = [
        req.script_set_id,
        str(req.script_set_revision),
        ",".join(req.requested_products),
        ",".join(f"{pid}:{dur}" for pid, dur in req.target_durations),
        str(req.max_product_concurrency),
        str(req.max_attempts),
        req.model_fingerprint,
        req.client_key,
    ]
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()


class IdempotencyRegistry:
    """In-memory + persisted idempotency registry (task 10.6).

    Maps a request fingerprint to an existing batch id so a repeated
    equivalent request returns the existing workflow instead of creating
    duplicate semantic jobs. Persistence is injected: ``save``/``load``
    round-trip the mapping so recovery survives process restart.
    """

    def __init__(self, store: Optional[dict] = None) -> None:
        # Keep a reference (not a copy) to the injected store so persistence
        # writes are immediately visible to a recovered instance — the same
        # contract a DB-backed store provides.
        self._entries = store if store is not None else {}

    @classmethod
    def from_persisted(cls, store: dict) -> "IdempotencyRegistry":
        return cls(store=store)

    def get(self, fingerprint: str) -> Optional[str]:
        """Return the existing batch id for ``fingerprint``, if any."""
        return self._entries.get(fingerprint)

    def register(self, fingerprint: str, batch_id: str) -> None:
        """Bind a fingerprint to a batch id (first registration wins)."""
        self._entries.setdefault(fingerprint, batch_id)

    def snapshot(self) -> dict:
        """Persistable mapping snapshot."""
        return dict(self._entries)


# ── orchestrator ──────────────────────────────────────────────────────────


@dataclass
class BatchOrchestratorConfig:
    """Batch orchestration configuration (defaults per spec)."""

    max_product_concurrency: int = 3
    max_attempts: int = 3
    model_fingerprint: str = ""

    def __post_init__(self) -> None:
        if self.max_product_concurrency < 1:
            raise ValueError("max_product_concurrency must be >= 1")
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")


class BatchScriptGenerationOrchestrator:
    """One bounded workflow per product, driven to completion by ``step``.

    The orchestrator owns scheduling, retry policy, idempotency,
    cancellation, and persistence — never the model's prose. Products are
    injected through ``create_workflow(product_id, target_duration_s)`` so
    cluster 9's ``ProductGenerationWorkflow`` (and test fakes) plug in
    without the batch importing their internals.
    """

    def __init__(
        self,
        create_workflow: Callable[[str, float], TWorkflow],
        *,
        config: BatchOrchestratorConfig | None = None,
        event_sink: EventSink = _default_event_sink,
        persist: Optional[Callable[[BatchState], None]] = None,
        idempotency: IdempotencyRegistry | None = None,
        scheduler: BoundedScheduler | None = None,
    ) -> None:
        self._create_workflow = create_workflow
        self._config = config or BatchOrchestratorConfig()
        self._event_sink = event_sink
        self._persist = persist
        self._idempotency = idempotency or IdempotencyRegistry()
        self._scheduler = scheduler or BoundedScheduler(self._config.max_product_concurrency)
        self._workflows: dict[str, TWorkflow] = {}
        self._state: Optional[BatchState] = None
        self._terminal_event_emitted = False

    # ── submission ─────────────────────────────────────────────────────

    def start(self, req: BatchRequest) -> tuple[BatchState, bool]:
        """Begin a batch, or return the existing one for a duplicate request.

        Returns ``(batch_state, created)`` where ``created`` is False when
        an equivalent batch is already queued/running (task 10.6) — no
        duplicate workflows or semantic jobs are created in that case.
        """
        fingerprint = request_fingerprint(req)
        existing = self._idempotency.get(fingerprint)
        if existing is not None:
            # Equivalent request already registered (running in this process
            # or recovered from a restart): refer to the existing workflow —
            # never create duplicate semantic jobs (task 10.6). The returned
            # state is a stable reference carrying the existing batch id; the
            # API layer fetches the full snapshot from persistence.
            return BatchState(
                batch_id=existing,
                script_set_id=req.script_set_id,
                status="queued",
                requested_products=list(req.requested_products),
                target_durations=dict(req.target_durations),
                preview={"products": [], "estimated_semantic_calls_total": 0},
            ), False

        batch_id = new_id("batch")
        self._state = self._build_state(req, batch_id)
        self._idempotency.register(fingerprint, batch_id)
        self._create_workflows(req)
        self._state.status = "queued"
        self._state.bump_revision()
        self._persist_state()
        self._emit("batch.started", {"batch_id": batch_id})
        return self._state, True

    def _build_state(self, req: BatchRequest, batch_id: str) -> BatchState:
        """Assemble the persisted batch state (task 10.3)."""
        preview_products: list[dict] = []
        planned_total = 0
        for product_id, duration in req.target_durations:
            k = max(1, round(duration / 600.0))
            planned = 1 + k  # planning call + K segments (Decision 7)
            preview_products.append(
                {
                    "product_id": product_id,
                    "target_duration_s": duration,
                    "planned_segment_count": k,
                    "estimated_semantic_calls": planned,
                }
            )
            planned_total += planned
        return BatchState(
            batch_id=batch_id,
            script_set_id=req.script_set_id,
            status="queued",
            requested_products=list(req.requested_products),
            target_durations=dict(req.target_durations),
            preview={
                "products": preview_products,
                "estimated_semantic_calls_total": planned_total,
            },
            products={pid: ProductWorkflowState(product_id=pid) for pid in req.requested_products},
            planned_semantic_calls=planned_total,
        )

    def _create_workflows(self, req: BatchRequest) -> None:
        """Create one finite workflow per product and enqueue it."""
        for product_id, duration in req.target_durations:
            workflow = self._create_workflow(product_id, duration)
            self._workflows[product_id] = workflow
            self._scheduler.enqueue(workflow)

    # ── execution ──────────────────────────────────────────────────────

    def step(self) -> BatchState:
        """Advance the batch one deterministic scheduler round.

        Promotes queued workflows into free slots (never more than
        ``max_product_concurrency``), runs exactly one finite step of each
        active workflow, and aggregates counts. Returns the current
        ``BatchState``. Safe to call when no batch is running: returns the
        existing state unchanged.
        """
        if self._state is None:
            raise RuntimeError("no batch started")
        if self._state.status in ("cancelled", "completed"):
            return self._state

        promoted = self._scheduler.promote()
        for workflow in promoted:
            state = self._product_state(workflow.product_id)
            state.status = "running"
            self._state.started += 1
            self._emit(
                "product.started",
                {"batch_id": self._state.batch_id, "product_id": workflow.product_id},
            )

        for workflow in self._scheduler.active():
            self._step_workflow(workflow)

        self._finalize_if_done()
        self._persist_state()
        return self._state

    def _step_workflow(self, workflow: TWorkflow) -> None:
        """Run one finite step of an active workflow with retry policy.

        Only ``TransportError`` is retried (up to ``max_attempts``) against
        the same immutable job input; a transport retry does not count as a
        semantic job (task 10.5). Content/gate failures never retry. A
        completed workflow frees its slot so the next queued product runs.
        """
        state = self._product_state(workflow.product_id)
        if state.status in ("completed", "failed", "cancelled"):
            return
        try:
            more = workflow.step()
        except TransportError:
            state.transport_attempts += 1
            if state.transport_attempts >= self._config.max_attempts:
                self._fail_product(workflow, "transport failure exceeded max_attempts")
            return  # same input will be retried on the next round
        except Exception as exc:  # content/gate or unexpected failure
            self._fail_product(workflow, str(exc) or type(exc).__name__)
            return

        self._sync_workflow_state(workflow, state)
        if not more:
            if workflow.is_terminal():
                self._complete_product(workflow)
            else:  # terminal-but-failed marker is carried by the workflow
                self._fail_product(workflow, "workflow ended without completion")

    def _sync_workflow_state(self, workflow: TWorkflow, state: ProductWorkflowState) -> None:
        """Copy the workflow's own finite counters into persisted state."""
        state.current_segment_index = int(getattr(workflow, "current_segment_index", 0))
        state.plan_segment_count = int(getattr(workflow, "plan_segment_count", 0))
        state.semantic_calls = int(getattr(workflow, "semantic_calls", 0))
        state.workflow_snapshot = dict(workflow.snapshot())
        # Aggregate actual semantic calls = sum over per-product counters.
        # Transport retries never increment these counters (task 10.5).
        self._state.actual_semantic_calls = sum(
            ps.semantic_calls for ps in self._state.products.values()
        )

    def _complete_product(self, workflow: TWorkflow) -> None:
        """Mark one product completed and free its scheduler slot."""
        state = self._product_state(workflow.product_id)
        state.status = "completed"
        self._sync_workflow_state(workflow, state)
        self._state.completed += 1
        self._scheduler.release(workflow)
        self._emit(
            "product.completed",
            {"batch_id": self._state.batch_id, "product_id": workflow.product_id},
        )

    def _fail_product(self, workflow: TWorkflow, reason: str) -> None:
        """Mark one product failed; completed siblings stay valid (10.4)."""
        state = self._product_state(workflow.product_id)
        if state.status in ("completed", "cancelled"):
            return
        state.status = "failed"
        state.error = reason
        self._sync_workflow_state(workflow, state)
        self._state.failed += 1
        self._scheduler.release(workflow)
        self._emit(
            "product.failed",
            {
                "batch_id": self._state.batch_id,
                "product_id": workflow.product_id,
                "reason": reason,
            },
        )

    def _finalize_if_done(self) -> None:
        """Compute aggregate status: completed / partial_completed / failed.

        One product's failure does not invalidate completed siblings: any
        completed product yields ``partial_completed`` when others failed or
        were cancelled (task 10.4). ``failed`` only when everything failed.
        """
        if self._scheduler.is_busy:
            return
        total = len(self._state.requested_products)
        done = self._state.completed + self._state.failed + self._state.cancelled
        if done < total:
            return  # queued items remain — not final yet
        if self._state.completed == total:
            self._state.status = "completed"
            self._emit_terminal("batch.completed")
        elif self._state.completed > 0:
            self._state.status = "partial_completed"
            self._emit_terminal("batch.partial_completed")
        else:
            self._state.status = "failed"
            self._emit_terminal("batch.failed")

    def _emit_terminal(self, event: str) -> None:
        """Emit a terminal batch event exactly once."""
        if self._terminal_event_emitted:
            return
        self._terminal_event_emitted = True
        self._emit(event, {"batch_id": self._state.batch_id})

    # ── cancellation (task 10.7) ───────────────────────────────────────

    def cancel(self) -> BatchState:
        """Stop scheduling new semantic calls and persist cancelled states.

        Completed immutable artifacts are preserved (their status stays
        ``completed``); queued and active workflows are marked cancelled. A
        terminal ``batch.cancelled`` event is emitted exactly once.
        """
        if self._state is None:
            raise RuntimeError("no batch started")
        if self._state.status in ("cancelled", "completed", "partial_completed"):
            return self._state

        for workflow in self._scheduler.drain_queued():
            state = self._product_state(workflow.product_id)
            if state.status == "queued":
                state.status = "cancelled"
                self._state.cancelled += 1
        for workflow in self._scheduler.active():
            state = self._product_state(workflow.product_id)
            if state.status not in ("completed", "failed"):
                state.status = "cancelled"
                self._state.cancelled += 1
            self._scheduler.release(workflow)

        self._state.status = "cancelled"
        self._state.bump_revision()
        self._persist_state()
        self._emit_terminal("batch.cancelled")
        return self._state

    # ── recovery (task 10.8) ───────────────────────────────────────────

    def restore(self, state: BatchState) -> None:
        """Rebuild the batch from a persisted ``BatchState``.

        Re-creates one workflow per requested product via the same
        ``create_workflow`` factory, restores each workflow's own finite
        snapshot (segment index etc.), and re-enqueues only incomplete
        products — completed segments are never re-run.
        """
        self._state = state.model_copy(deep=True)
        self._terminal_event_emitted = False
        self._workflows = {}
        self._scheduler = BoundedScheduler(self._config.max_product_concurrency)
        for product_id in self._state.requested_products:
            ps = self._state.products[product_id]
            duration = self._state.target_durations.get(product_id, 600.0)
            workflow = self._create_workflow(product_id, duration)
            if ps.workflow_snapshot:
                workflow.restore(dict(ps.workflow_snapshot))
            self._workflows[product_id] = workflow
            if ps.status in ("queued", "running"):
                self._scheduler.enqueue(workflow)
            # completed/failed/cancelled products never re-enter the queue.

    # ── accessors ──────────────────────────────────────────────────────

    @property
    def state(self) -> Optional[BatchState]:
        return self._state

    @property
    def scheduler(self) -> BoundedScheduler:
        return self._scheduler

    @property
    def workflows(self) -> dict[str, TWorkflow]:
        return dict(self._workflows)

    def _product_state(self, product_id: str) -> ProductWorkflowState:
        assert self._state is not None, "batch not started"
        return self._state.products[product_id]

    def _persist_state(self) -> None:
        if self._persist is not None and self._state is not None:
            self._persist(self._state)

    def _emit(self, event: str, payload: dict) -> None:
        self._event_sink(event, payload)


def recover_batch(
    orchestrator: BatchScriptGenerationOrchestrator,
    persisted_state: BatchState,
) -> BatchScriptGenerationOrchestrator:
    """Recover a batch from persisted state (task 10.8, module function).

    Resumes from the persisted finite next step/segment index — it never
    reinterprets model prose and never re-runs completed segments. Returns
    the orchestrator for chaining.
    """
    orchestrator.restore(persisted_state)
    return orchestrator
