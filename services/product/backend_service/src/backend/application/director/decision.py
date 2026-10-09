"""Director — backend-agnostic orchestration FSM.

Sits between viewer comments and the RenderBackend. Decides WHAT to say and
WHEN; never renders. Pure-logic + deterministic (clock is injected), so it is
unit-testable offline and reusable across cloud/self-host renderers.

Per-cycle flow (one cycle = decide the next thing the avatar says):
  1. ingest comments in the selection window -> embed -> cluster
  2. rank clusters (retrieval + phase/intent/size/recency score)
  3. phase logic:
       OPENING  -> emit a hook from the pre-generated pool until timeout/viewers
       SELLING  -> answer the top cluster for the current product; switch product
                   on OR(time-budget, engagement-decay, max-clusters); allow
                   "go back to product X" via retrieval/explicit id
       CLOSING  -> wrap up
  4. return a Decision (action + text-intent + whether it may interrupt)

The Director produces a Decision; the caller feeds Decision.prompt to the LLM
(or uses Decision.text directly for templated hooks) and the reply to the
RenderBackend.say(). Interrupt gate (challenge: barge-in) = only a cluster
scoring above cfg.interrupt_score_threshold may cut off the avatar.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Optional, Sequence
from uuid import uuid4

from backend.application.entity.models import EntityDocument

from .catalog import answer_field, embedding_text, route_intent_to_field
from .clustering import Comment, cluster_comments
from .config import StreamConfig
from .hooks import HookPool
from .pivot import should_enter_pivot, should_exit_pivot
from .scoring import ScoredCluster, rank_clusters
from .state import Phase, ProductStatus, StreamState


@dataclass
class Decision:
    """What the Director wants the avatar to do this cycle."""

    action: str  # "speak_hook" | "answer_cluster" | "answer_fact" | "introduce_product" | "close" | "idle"
    text: Optional[str] = None  # for templated hooks / O(1) factual answers (no LLM)
    prompt: Optional[str] = None  # for LLM-generated answers
    product_id: Optional[str] = None
    field: Optional[str] = None  # structured attribute answered (if action == answer_fact)
    may_interrupt: bool = False
    reason: str = ""
    # Structured decision score (set from the ranked cluster score for
    # answer_fact/answer_cluster; 0.0 for hooks/idle/introduce). Used by the
    # coordinator for interrupt arbitration without parsing `reason`.
    score: float = 0.0
    cluster_members: tuple[str, ...] = ()
    cluster_member_ids: tuple[str, ...] = ()
    stage: Optional[str] = None
    task_id: Optional[str] = None
    prompt_layers: dict[str, str] = dataclass_field(default_factory=dict)
    generation_token: int = 0
    revision_token: str = ""
    prepared_script: Optional[str] = None
    prepared_variants: tuple[str, ...] = ()
    approved_speech: object = None
    # Approved-script unit this turn speaks (index into ProductState.units).
    unit_index: Optional[int] = None
    prepared_from_projection: bool = False
    is_cancelled: bool = False
    attempt: int = 0
    cache_variant_index: Optional[int] = None
    excursion: bool = False
    resume_product_id: Optional[str] = None
    pivot: bool = False
    queued_pivot_products: tuple[str, ...] = ()
    topic: Optional[str] = None
    score_breakdown: dict[str, float] = dataclass_field(default_factory=dict)
    decided_at: float = 0.0
    completed_at: float = 0.0
    qa_window_open_after_decision: Optional[bool] = None
    qa_window_started_at_after_decision: Optional[float] = None
    qa_window_stage_index_after_decision: Optional[int] = None
    qa_clusters_answered_after_decision: Optional[int] = None
    latency_spans: dict[str, dict[str, float]] = dataclass_field(default_factory=dict)
    turn_id: str = dataclass_field(default_factory=lambda: uuid4().hex)
    # Reducer provenance (P0-FB-014). It travels BESIDE the envelope, never
    # inside it: the envelope's trust boundary excludes member/viewer ids, so
    # these three fields are the audit/freshness/idempotency record.
    source_cluster_id: Optional[str] = None
    source_event_ids: tuple[str, ...] = ()
    occurred_at_bounds: tuple[float, float] = (0.0, 0.0)

    def provenance_payload(self) -> dict:
        """The provenance block carried by events and the persisted decision row."""
        return {
            "cluster_id": self.source_cluster_id,
            "source_event_ids": list(self.source_event_ids),
            "oldest_occurred_at": self.occurred_at_bounds[0],
            "newest_occurred_at": self.occurred_at_bounds[1],
        }


class Director:
    """Orchestrates one live session."""

    def __init__(
        self,
        state: StreamState,
        cfg: Optional[StreamConfig] = None,
        hook_pool: Optional[HookPool] = None,
        catalog: Optional[dict[str, EntityDocument]] = None,
    ) -> None:
        self.state = state
        self.cfg = cfg or StreamConfig()
        self.hooks = hook_pool or HookPool()
        # product_id -> EntityDocument, for O(1) factual answers (TIER 2).
        self.catalog = catalog or {}
        # P0-FB-014 high-value predicate. REQUIRES_VALIDATION implementation
        # default (brief §human decision 2), NOT a Product Rule: a cluster whose
        # reducer intent clears ``high_value_threshold`` in the existing
        # ``_INTENT_ACTIONABILITY`` table, plus SafetyGate-flagged safety intent.
        # The 0.8 default is exactly the brief's proposed set — price/stock/
        # buy_intent (1.0), comparison (0.9), complaint (0.8).
        self.high_value_threshold: float = 0.8
        self.safety_intents: frozenset[str] = frozenset()

    def is_high_value(self, envelope: Any) -> bool:
        """Whether one reducer envelope clears the high-value predicate.

        REQUIRES_VALIDATION default: actionability at or above the configured
        threshold in ``reducer.demand._INTENT_ACTIONABILITY``, or a
        SafetyGate-flagged safety intent that reached the reducer. A runtime
        default, never promoted to a Product Rule.
        """
        from ..reducer.demand import _INTENT_ACTIONABILITY

        if envelope.intent in self.safety_intents:
            return True
        weight = _INTENT_ACTIONABILITY.get(envelope.intent, 0.3)
        return weight >= self.high_value_threshold

    def high_value_cluster_ids(self, selections: Sequence[Any]) -> set[str]:
        return {s.envelope.cluster_id for s in selections if self.is_high_value(s.envelope)}

    # ── phase transitions ────────────────────────────────────────────

    def _maybe_leave_opening(self) -> None:
        s = self.state
        if s.phase != Phase.OPENING:
            return
        if s.cursor.opening_completed or s.cursor.opening_turn_index >= 3:
            s.cursor.opening_completed = True
            s.phase = Phase.SELLING
            s.cursor.phase = "selling"
            s.phase_elapsed_sec = 0.0
            cur = s.current_product()
            if cur:
                cur.status = ProductStatus.ACTIVE

    def _opening_turn(self) -> Decision:
        index = self.state.cursor.opening_turn_index
        hook = self.hooks.get("opening", index)
        return Decision(
            action="speak_hook",
            text=hook,
            stage="opening",
            task_id=f"opening:{index + 1}",
            reason="protected global opening",
            score=0.0,
        )

    def _mark_opening_spoken(self) -> None:
        self.state.cursor.opening_turn_index += 1
        if self.state.cursor.opening_turn_index >= 3:
            self.state.cursor.opening_completed = True
            self.state.phase = Phase.SELLING
            self.state.cursor.phase = "selling"
            self.state.phase_elapsed_sec = 0.0
            cur = self.state.current_product()
            if cur:
                cur.status = ProductStatus.ACTIVE

    def _should_switch_product(self) -> bool:
        """Honor the hard budget; use soft gates only after planned sales turns."""
        s, c = self.state, self.cfg
        cur = s.current_product()
        if cur is None or s.cursor.pivot_active:
            return False
        if cur.units and not self._units_exhausted(cur):
            # The owner's ordered parts must all be spoken: no budget/decay skips.
            return False
        if s.product_elapsed_sec >= c.product_time_budget_sec:
            return True
        tasks = self._sales_tasks(cur.product_id)
        if tasks and cur.stage_turn_index < len(tasks):
            return False
        return (
            s.sec_since_relevant_msg >= c.engagement_decay_sec
            or cur.cluster_count >= c.max_clusters_per_product
        )

    def _advance_product(self) -> None:
        s = self.state
        cur = s.current_product()
        if cur:
            cur.status = ProductStatus.DONE
        # next pending product in order
        for i in range(s.current_product_index + 1, len(s.products)):
            if s.products[i].status != ProductStatus.DONE:
                s.current_product_index = i
                s.products[i].status = ProductStatus.ACTIVE
                s.product_elapsed_sec = 0.0
                s.sec_since_relevant_msg = 0.0
                cur2 = s.current_product()
                if cur2 is not None:
                    cur2.cluster_count = 0
                return
        # nothing left -> closing
        s.phase = Phase.CLOSING

    # ── main decision ────────────────────────────────────────────────

    def decide(self, comments: list[Comment], now: float) -> Decision:
        """Produce the next Decision given recent comments and the clock."""
        pivot_queue_before = set(self.state.cursor.pivot_queue)
        return self._stamp(self._decide(comments, now), now, pivot_queue_before)

    def _stamp(
        self,
        decision: Decision,
        now: float,
        pivot_queue_before: set,
    ) -> Decision:
        """Stamp the decision with the clock and the post-decision snapshots.

        Shared by both feeds. ``mark_spoken`` replays the Q&A window state and
        the pivot queue from these fields, and the topic cooldown is computed
        from ``decided_at`` — so every decision must carry them.
        """
        decision.decided_at = now
        decision.queued_pivot_products = tuple(
            product_id
            for product_id in self.state.cursor.pivot_queue
            if product_id not in pivot_queue_before
        )
        decision.qa_window_open_after_decision = self.state.qa_window_open
        decision.qa_window_started_at_after_decision = self.state.qa_window_started_at
        decision.qa_window_stage_index_after_decision = self.state.qa_window_stage_index
        decision.qa_clusters_answered_after_decision = self.state.qa_clusters_answered
        return decision

    def decide_from_reducer(
        self,
        selections: Sequence[Any],
        now: float,
        *,
        high_value_ids: Optional[Callable[[str], bool]] = None,
    ) -> Decision:
        """Produce the next Decision from bounded reducer output (P0-FB-014).

        The reducer is the only viewer-demand input for a P0 session, so no raw
        comment is clustered or ranked here: ``build_selections`` already
        projected the live clusters, and ``select_scored`` reused the legacy
        ranking. Everything else — pivot checkpoint, no nested pivot, cooldown
        and signature suppression, ``mark_answered``, the protected opening —
        is the same code the legacy feed runs.
        """
        from .reducer_input import attach_provenance, select_scored

        pivot_queue_before = set(self.state.cursor.pivot_queue)
        by_cluster = {s.envelope.cluster_id: s for s in selections}
        ranked = select_scored(self, selections, now=now)
        high_value_ids = high_value_ids or (lambda _cid: False)
        decision = self._decide(
            [],
            now,
            ranked=ranked,
            by_cluster=by_cluster,
            is_high_value=high_value_ids,
        )
        if decision.source_cluster_id is not None:
            attach_provenance(decision, by_cluster)
        return self._stamp(decision, now, pivot_queue_before)

    def _decide(
        self,
        comments: list[Comment],
        now: float,
        *,
        ranked: Optional[list[ScoredCluster]] = None,
        by_cluster: Optional[dict[str, Any]] = None,
        is_high_value: Optional[Callable[[str], bool]] = None,
    ) -> Decision:
        """The one decision function; ``ranked`` is the reducer-mode injection.

        Passing ``ranked`` short-circuits clustering and ranking entirely: the
        bounded reducer already produced the clusters, so the raw-comment feed
        is never read for that session. ``None`` is the legacy path, unchanged.
        """
        s, c = self.state, self.cfg
        reducer_mode = ranked is not None
        high_value = is_high_value or (lambda _cid: False)
        by_cluster = by_cluster or {}
        by_members: dict[tuple[str, ...], str] = {}

        # OPENING: three protected grounded turns; comments cannot interrupt.
        # No reducer selection preempts the 007 approved opening (the human
        # product decision on that remains open; the opening stays protected).
        self._maybe_leave_opening()
        if s.phase == Phase.OPENING:
            return self._opening_turn()

        if s.phase == Phase.CLOSING:
            if s.closing_spoken:
                return Decision(action="idle", reason="closing already spoken", score=0.0)
            return self._close_decision("closing phase", "closing")

        # SELLING
        if not reducer_mode:
            window = [cm for cm in comments if now - cm.t <= c.selection_window_sec]
            clusters = cluster_comments(window, merge_threshold=c.cluster_merge_threshold)
            ranked = [
                item
                for item in rank_clusters(clusters, s, c, now)
                if not item.cluster.member_ids
                or not all(
                    member_id in s.answered_comments for member_id in item.cluster.member_ids
                )
            ]
        else:
            window = []
            clusters = []
            # Identity index: reducer cluster id by member-id tuple. Built here
            # from the adapter output, so legacy clusters are absent from it —
            # which is exactly what keeps the high-value relaxation shut for the
            # legacy feed.
            by_members = {
                selection.member_comment_ids: cluster_id
                for cluster_id, selection in by_cluster.items()
            }
        relevant_ages = [
            max(0.0, now - item.cluster.newest_t)
            for item in ranked
            if item.cluster.product_id in self.catalog
        ]
        if relevant_ages:
            s.sec_since_relevant_msg = min(s.sec_since_relevant_msg, min(relevant_ages))

        # Fresh relevant demand must reach ranking before the engagement-decay gate.
        if self._should_switch_product():
            self._advance_product()
            if s.phase == Phase.CLOSING:
                return self._close_decision("all products done", None)
            if reducer_mode:
                # Same re-filter as the legacy re-rank, minus a re-cluster: the
                # reducer clusters are the only ones and they already exist.
                ranked = [
                    item
                    for item in ranked
                    if not any(
                        member_id in s.answered_comments for member_id in item.cluster.member_ids
                    )
                ]
            else:
                ranked = [
                    item
                    for item in rank_clusters(clusters, s, c, now)
                    if not any(
                        member_id in s.answered_comments for member_id in item.cluster.member_ids
                    )
                ]

        cur = s.current_product()
        actionable_product_ids = (
            [
                item.cluster.product_id
                for item in ranked
                if item.cluster.actionable and item.cluster.product_id is not None
            ]
            if reducer_mode
            else [
                comment.product_id
                for comment in window
                if comment.actionable and comment.product_id is not None
            ]
        )
        eligible = []
        if cur is not None:
            for item in ranked:
                high = reducer_mode and high_value(_cluster_id_of(by_members, item))
                # The legacy size gate drops every singleton, which is how a
                # lone high-value or safety question used to be lost (BR-QA-001).
                # It is relaxed for high-value clusters only.
                if item.cluster.size < 2 and not high:
                    continue
                product_id = item.cluster.product_id or cur.product_id
                topic_key = f"{product_id}:{item.cluster.intent}"
                prior_signature = s.qa_last_comment_signature.get(topic_key)
                prior_members = set(prior_signature.split("\n")) if prior_signature else set()
                current_members = set(item.cluster.members)
                novel_members = current_members - prior_members
                has_new_content = len(novel_members) >= 2
                if now < s.topic_cooldown_until.get(topic_key, 0.0) and not has_new_content:
                    continue
                eligible.append(item)
        ranked = eligible

        if s.cursor.pivot_active and s.cursor.pivot_product_id:
            pivot_id = s.cursor.pivot_product_id
            for product_id in dict.fromkeys(actionable_product_ids):
                if product_id not in (pivot_id, s.cursor.checkpoint_product_id):
                    if product_id not in s.cursor.pivot_queue:
                        s.cursor.pivot_queue.append(product_id)
            pivot_product = s.current_product()
            pivot_lifecycle_complete = bool(
                pivot_product
                and pivot_product.product_id == pivot_id
                and (pivot_product.is_introduced or self._units_exhausted(pivot_product))
                and (
                    self._units_exhausted(pivot_product)
                    if pivot_product.units
                    else pivot_product.stage_turn_index >= len(self._sales_tasks(pivot_id))
                )
            )
            if (
                pivot_lifecycle_complete
                and actionable_product_ids
                and should_exit_pivot(
                    pivot_id,
                    actionable_product_ids,
                    exit_share=c.demand_pivot_exit_share,
                )
            ):
                resume_id = s.cursor.checkpoint_product_id
                return Decision(
                    action="resume_product",
                    product_id=resume_id,
                    stage="resume",
                    task_id=f"{resume_id}:resume" if resume_id else "resume",
                    resume_product_id=resume_id,
                    reason="pivot lifecycle completed and demand cooled below exit threshold",
                )
        elif cur is not None:
            cross_product = next(
                (item for item in ranked if item.cluster.product_id not in (None, cur.product_id)),
                None,
            )
            if cross_product is not None:
                target_id = cross_product.cluster.product_id
                total_demand = max(len(actionable_product_ids), 1)
                target_share = actionable_product_ids.count(target_id) / total_demand
                current_share = actionable_product_ids.count(cur.product_id) / total_demand
                pivot = should_enter_pivot(
                    target_id or "",
                    actionable_product_ids,
                    min_comments=c.demand_pivot_min_comments,
                    enter_share=c.demand_pivot_enter_share,
                    score_margin=c.demand_pivot_score_margin,
                    top_score=target_share,
                    current_score=current_share,
                )
                cross_selection = by_cluster.get(_cluster_id_of(by_members, cross_product))
                cross_decision = self._qa_decision(
                    cross_product,
                    cur,
                    pivot=pivot,
                    excursion=not pivot,
                )
                if cross_selection is not None:
                    cross_decision.source_cluster_id = cross_selection.envelope.cluster_id
                return cross_decision

        if cur is not None and (
            not s.qa_window_open
            and cur.stage_turn_index >= 2
            and s.qa_window_stage_index != cur.stage_turn_index
        ):
            s.qa_window_open = True
            s.qa_clusters_answered = 0
            s.qa_window_started_at = now
            s.qa_window_stage_index = cur.stage_turn_index
        if s.qa_window_open and (
            s.qa_clusters_answered >= c.max_qa_clusters_per_window
            or now - s.qa_window_started_at >= c.qa_window_hard_timeout_sec
            or not ranked
        ):
            s.qa_window_open = False
            ranked = []
        elif not s.qa_window_open:
            # A closed Q&A window drops everything EXCEPT a high-value cluster
            # (BR-QA-001/002): safety and purchase intent must not be blocked
            # solely by a legacy stage. It becomes eligible at this safe
            # boundary, with every other safeguard unchanged.
            ranked = [item for item in ranked if high_value(_cluster_id_of(by_members, item))]
        if (
            cur is not None
            and not cur.is_introduced
            and not self._units_exhausted(cur)
            and not _any_high(ranked, by_members, high_value)
        ):
            return self._introduce_decision(cur, "introduce current product before viewer Q&A")

        # No viewer question: keep selling the current product one short stage
        # at a time. When its stage plan is exhausted, advance in operator order.
        if not ranked:
            proactive = self._next_sales_turn(cur)
            if proactive is not None:
                return proactive
            self._advance_product()
            # A product whose units were all spoken (pivot revisit) is not replayed.
            while s.phase != Phase.CLOSING and self._units_exhausted(s.current_product()):
                self._advance_product()
            if s.phase == Phase.CLOSING:
                return self._close_decision("all product sales stages completed", "closing")
            next_product = s.current_product()
            if next_product is not None:
                return self._introduce_decision(
                    next_product, "advance to next product after sales stages"
                )
            return Decision(action="idle", reason="no product available", score=0.0)

        # Alternate Q&A with proactive selling so a busy comment stream cannot
        # reduce a product to one intro followed by endless answers. Relaxed for
        # high-value clusters so a busy stream cannot starve a safety question.
        if (
            cur is not None
            and cur.reactive_streak >= 1
            and not _any_high(ranked, by_members, high_value)
        ):
            proactive = self._next_sales_turn(cur)
            if proactive is not None:
                return proactive

        top = ranked[0]
        for skipped in ranked[1:]:
            skipped.cluster.skips += 1
        selection = by_cluster.get(_cluster_id_of(by_members, top))
        decision = self._qa_decision(top, cur)
        if selection is not None:
            decision.source_cluster_id = selection.envelope.cluster_id
        return decision

    def _qa_decision(
        self,
        top: ScoredCluster,
        current,
        *,
        pivot: bool = False,
        excursion: bool = False,
    ) -> Decision:
        if top.cluster.product_id is not None:
            self.state.sec_since_relevant_msg = 0.0
        product_id = top.cluster.product_id or (current.product_id if current is not None else None)
        topic = top.cluster.intent or "unknown"
        resume_id = (
            current.product_id if current is not None and product_id != current.product_id else None
        )
        cache_key = (
            product_id or "unknown",
            topic,
            self.state.cursor.profile_revision,
            self.state.cursor.catalog_revision,
        )
        variants = self.state.answer_variants.get(cache_key) or []
        variant_index = self.state.answer_variant_index.get(cache_key, 0)
        cached_script = variants[variant_index % len(variants)] if variants else None
        field_name = self._route_field(top)
        fact = (
            answer_field(self.catalog[product_id], field_name)
            if (field_name and product_id in self.catalog)
            else None
        )
        action = "answer_fact" if fact else "answer_cluster"
        prompt = (
            self._grounded_prompt(top, product_id, field_name, fact)
            if fact
            else self._answer_prompt(top)
        )
        return Decision(
            action=action,
            prompt=None if cached_script is not None else prompt,
            text=fact,
            prepared_script=cached_script,
            cache_variant_index=variant_index % len(variants) if variants else None,
            product_id=product_id,
            field=field_name if fact else None,
            may_interrupt=top.score >= self.cfg.interrupt_score_threshold,
            reason=f"top cluster score={top.score:.2f}",
            score=top.score,
            stage="qa",
            task_id=f"{product_id or 'current'}:qa:{top.cluster.member_ids[0]}",
            cluster_members=tuple(top.cluster.members),
            cluster_member_ids=tuple(top.cluster.member_ids),
            topic=topic,
            score_breakdown=top.breakdown(),
            excursion=excursion,
            resume_product_id=resume_id,
            pivot=pivot,
        )

    def mark_spoken(self, decision: Decision) -> None:
        """Record a completed opening, sales turn, or reactive answer."""
        for product_id in decision.queued_pivot_products:
            if product_id not in self.state.cursor.pivot_queue:
                self.state.cursor.pivot_queue.append(product_id)
        if decision.qa_window_open_after_decision is not None:
            self.state.qa_window_open = decision.qa_window_open_after_decision
        if decision.qa_window_started_at_after_decision is not None:
            self.state.qa_window_started_at = decision.qa_window_started_at_after_decision
        if decision.qa_window_stage_index_after_decision is not None:
            self.state.qa_window_stage_index = decision.qa_window_stage_index_after_decision
        if decision.qa_clusters_answered_after_decision is not None:
            self.state.qa_clusters_answered = decision.qa_clusters_answered_after_decision
        if decision.action == "resume_product" and decision.resume_product_id:
            self._resume_checkpoint()
            return
        if decision.pivot and decision.product_id:
            self._start_pivot(decision.product_id)
        if decision.unit_index is not None and decision.product_id:
            for product in self.state.products:
                if product.product_id == decision.product_id:
                    product.next_unit = max(product.next_unit, decision.unit_index + 1)
                    break
        if decision.stage == "opening":
            if decision.action == "autonomous_opening":
                # P0 has one complete approved opening, not three template hooks.
                self.state.cursor.opening_turn_index = 2
            self._mark_opening_spoken()
        if decision.product_id and decision.action in (
            "introduce_product",
            "sell_product",
        ):
            current = self.state.current_product()
            if current is None or current.product_id != decision.product_id:
                if current is not None and not decision.pivot and not decision.excursion:
                    current.status = ProductStatus.DONE
                self.state.goto_product(decision.product_id)
            active_product = self.state.current_product()
            if active_product is not None:
                active_product.status = ProductStatus.ACTIVE
            for product in self.state.products:
                if product.product_id != decision.product_id:
                    continue
                product.spoken_turns += 1
                product.stage = decision.stage or product.stage
                if decision.action == "introduce_product":
                    product.is_introduced = True
                    product.stage_turn_index = max(product.stage_turn_index, 1)
                else:
                    product.stage_turn_index += 1
                product.reactive_streak = 0
                break
        if decision.action in ("answer_fact", "answer_cluster"):
            self.state.answered_comments.update(
                decision.cluster_member_ids or decision.cluster_members
            )
            topic = decision.topic or decision.field or "unknown"
            product_id = decision.product_id or (
                self.state.current_product().product_id
                if self.state.current_product() is not None
                else "unknown"
            )
            topic_key = f"{product_id}:{topic}"
            self.state.qa_clusters_answered += 1
            self.state.topic_cooldown_until[topic_key] = (
                decision.completed_at or decision.decided_at
            ) + self.cfg.qa_topic_cooldown_sec
            self.state.qa_last_comment_signature[topic_key] = "\n".join(
                sorted(set(decision.cluster_members))
            )
            cache_key = (
                product_id,
                topic,
                self.state.cursor.profile_revision,
                self.state.cursor.catalog_revision,
            )
            if decision.prepared_script and decision.cache_variant_index is None:
                variants = self.state.answer_variants.setdefault(cache_key, [])
                candidates = decision.prepared_variants or (decision.prepared_script,)
                for candidate in candidates:
                    if candidate not in variants:
                        variants.append(candidate)
                del variants[self.cfg.answer_cache_variants :]
                self.state.answer_variant_index[cache_key] = 1 % max(len(variants), 1)
            elif decision.cache_variant_index is not None:
                variants = self.state.answer_variants.get(cache_key) or []
                if variants:
                    self.state.answer_variant_index[cache_key] = (
                        decision.cache_variant_index + 1
                    ) % len(variants)
            self.state.qa_window_open = (
                self.state.qa_clusters_answered < self.cfg.max_qa_clusters_per_window
            )
            current = self.state.current_product()
            if current is not None:
                current.cluster_count += 1
                current.reactive_streak += 1
            if decision.excursion and decision.resume_product_id:
                self.state.goto_product(decision.resume_product_id)
        if decision.action == "close":
            for product in self.state.products:
                if product.status == ProductStatus.ACTIVE:
                    product.status = ProductStatus.DONE
            self.state.phase = Phase.CLOSING
            self.state.cursor.phase = "closing"
            self.state.closing_spoken = True

    def _start_pivot(self, product_id: str) -> None:
        current = self.state.current_product()
        if current is None or current.product_id == product_id:
            return
        cursor = self.state.cursor
        cursor.checkpoint_product_id = current.product_id
        cursor.checkpoint_stage = current.stage
        cursor.checkpoint_turn_index = current.stage_turn_index
        cursor.pivot_product_id = product_id
        cursor.pivot_active = True
        cursor.pivot_completed = False
        self.state.goto_product(product_id)
        pivot_product = self.state.current_product()
        if pivot_product is not None:
            pivot_product.status = ProductStatus.ACTIVE
            pivot_product.is_introduced = False
            pivot_product.stage = "intro"
            pivot_product.stage_turn_index = 0
            pivot_product.spoken_turns = 0
            pivot_product.reactive_streak = 0
        self.state.qa_window_open = False

    def _resume_checkpoint(self) -> None:
        cursor = self.state.cursor
        product_id = cursor.checkpoint_product_id
        if product_id is None or not self.state.goto_product(product_id):
            return
        product = self.state.current_product()
        if product is not None:
            product.status = ProductStatus.ACTIVE
            product.stage = cursor.checkpoint_stage or product.stage
            product.stage_turn_index = cursor.checkpoint_turn_index
        cursor.pivot_active = False
        cursor.pivot_completed = True
        cursor.pivot_product_id = None
        cursor.checkpoint_product_id = None
        cursor.checkpoint_stage = None
        cursor.checkpoint_turn_index = 0
        self.state.qa_window_open = False

    def mark_answered(self, decision: Decision) -> None:
        """Backward-compatible alias for completed reactive decisions."""
        self.mark_spoken(decision)

    @staticmethod
    def _route_field(top: ScoredCluster) -> Optional[str]:
        """Map a cluster to a structured attribute field (first member that hits)."""
        for m in top.cluster.members:
            f = route_intent_to_field(m)
            if f:
                return f
        return None

    # ── prompt builders (fed to the LLM) ─────────────────────────────

    def _sales_tasks(self, product_id: str) -> list[tuple[str, str, str]]:
        plan = self.state.run_plan
        selling = getattr(plan, "selling", None)
        if selling is None and isinstance(plan, dict):
            selling = plan.get("selling") or []
        for phase in selling or []:
            pid = phase.product_id if hasattr(phase, "product_id") else phase.get("product_id")
            if pid != product_id:
                continue
            tasks = phase.tasks if hasattr(phase, "tasks") else phase.get("tasks") or []
            return [
                (
                    task.stage if hasattr(task, "stage") else task.get("stage"),
                    task.task_id if hasattr(task, "task_id") else task.get("task_id"),
                    task.instruction if hasattr(task, "instruction") else task.get("instruction"),
                )
                for task in tasks
            ]
        return []

    def _next_sales_turn(self, product) -> Optional[Decision]:
        if product is None:
            return None
        if product.units:
            if self._units_exhausted(product):
                return None
            return self._unit_decision(
                product, "sell_product", "benefit", f"continue product unit {product.next_unit}"
            )
        tasks = self._sales_tasks(product.product_id)
        index = product.stage_turn_index
        if not tasks:
            if index > 1:
                return None
            fallback = [
                (
                    "intro",
                    f"{product.product_id}:intro:fallback",
                    f"Định vị {product.name} bằng một câu ngắn.",
                ),
                (
                    "benefit",
                    f"{product.product_id}:benefit:fallback",
                    f"Nêu một lợi ích nổi bật của {product.name}.",
                ),
                ("offer", f"{product.product_id}:offer:fallback", "Nêu giá và ưu đãi rõ ràng."),
                (
                    "trust",
                    f"{product.product_id}:trust:fallback",
                    "Nêu một thông tin tạo tin cậy cho sản phẩm.",
                ),
                ("cta", f"{product.product_id}:cta:fallback", "Kêu gọi chốt đơn tự nhiên."),
            ]
            tasks = fallback
        # Coverage read (P0-FB-014): ``_after_speak`` already writes the covered
        # key points per product; until now nothing read them. Skip a stage
        # whose key points are fully covered instead of repeating must-cover
        # content, and advance so progression still reaches later products.
        index = self._first_uncovered_stage(product, tasks, index)
        if index >= len(tasks):
            return None
        stage, task_id, instruction = tasks[index]
        return Decision(
            action="sell_product",
            prompt=self._stage_prompt(product, stage, instruction),
            product_id=product.product_id,
            stage=stage,
            task_id=task_id,
            reason=f"continue product sales stage {stage}",
        )

    # -- approved-script units ------------------------------------------

    def _unit_limit(self, product) -> Optional[int]:
        """Units played as intro/sell turns; None = legacy product without units.

        The last unit of the LAST product is reserved: it is the session closing.
        """
        if product is None or not product.units:
            return None
        last = bool(self.state.products) and self.state.products[-1] is product
        return len(product.units) - 1 if last and len(product.units) > 1 else len(product.units)

    def _units_exhausted(self, product) -> bool:
        limit = self._unit_limit(product)
        return limit is not None and product.next_unit >= limit

    def _unit_decision(self, product, action: str, stage: str, reason: str) -> Decision:
        index = product.next_unit
        text = product.units[index]
        return Decision(
            action=action,
            text=text,
            prepared_script=text,
            product_id=product.product_id,
            stage=stage,
            task_id=f"{product.product_id}:unit:{index}",
            unit_index=index,
            reason=reason,
            score=0.0,
        )

    def _introduce_decision(self, product, reason: str) -> Decision:
        if product.units:
            return self._unit_decision(product, "introduce_product", "intro", reason)
        return Decision(
            action="introduce_product",
            prompt=self._introduce_prompt(product),
            product_id=product.product_id,
            stage="intro",
            task_id=f"{product.product_id}:intro",
            reason=reason,
            score=0.0,
        )

    def _close_decision(self, reason: str, stage: Optional[str]) -> Decision:
        last = self.state.products[-1] if self.state.products else None
        if last is not None and len(last.units) > 1:
            index = len(last.units) - 1
            if last.next_unit <= index:
                text = last.units[index]
                return Decision(
                    action="close",
                    text=text,
                    prepared_script=text,
                    product_id=last.product_id,
                    stage="closing",
                    task_id=f"{last.product_id}:unit:{index}",
                    unit_index=index,
                    reason=reason,
                    score=0.0,
                )
        return Decision(
            action="close",
            text=self.hooks.next_hook("closing"),
            stage=stage,
            reason=reason,
            score=0.0,
        )

    def _covered_key_points(self, product_id: str) -> list[str]:
        """The run plan's key selling points for one product (empty if none)."""
        plan = self.state.run_plan
        if plan is None:
            return []
        selling = getattr(plan, "selling", None)
        if selling is None and isinstance(plan, dict):
            selling = plan.get("selling") or []
        for phase in selling or []:
            pid = phase.product_id if hasattr(phase, "product_id") else phase.get("product_id")
            if pid == product_id:
                points = (
                    phase.key_selling_points
                    if hasattr(phase, "key_selling_points")
                    else phase.get("key_selling_points") or []
                )
                return list(points)
        return []

    def _first_uncovered_stage(self, product, tasks, index: int) -> int:
        """First stage at or after ``index`` whose key points are not all covered.

        A stage with no key points is always eligible — coverage is only
        evidence for the stages that actually declare what they must say.
        """
        covered = self.state.covered_points.get(product.product_id) or set()
        if not covered:
            return index
        points = self._covered_key_points(product.product_id)
        if not points:
            return index
        from .scoring import coverage_ratio

        if coverage_ratio(covered, points) >= 1.0:
            # The whole product is covered; the caller advances to the next one.
            return len(tasks)
        # Only skip forward while a stage is a strict repeat of covered ground.
        while index < len(tasks) and _stage_is_covered(tasks[index], covered):
            index += 1
        return index

    def _stage_prompt(self, product, stage: str, instruction: str) -> str:
        catalog_product = self.catalog.get(product.product_id)
        facts = embedding_text(catalog_product) if catalog_product else product.name
        return (
            f"Nhiệm vụ stage {stage}: {instruction}. "
            "Chỉ tạo một turn từ 1 đến 3 câu hoàn chỉnh, có nhịp nói tự nhiên. "
            "Không bỏ dở câu, số tiền hoặc đơn vị. Không viết toàn bộ kịch bản sản phẩm. "
            f"Sản phẩm và dữ liệu được phép dùng: {facts}."
        )

    def _introduce_prompt(self, product) -> str:
        if product is None:
            return "Mở sản phẩm hiện tại bằng 1 đến 2 câu hoàn chỉnh."
        catalog_product = self.catalog.get(product.product_id)
        if catalog_product is None:
            return (
                f"Mở sản phẩm '{product.name}' bằng 1 đến 2 câu hoàn chỉnh, "
                "tự nhiên như MC livestream. Chỉ tạo tò mò, chưa kể hết mọi thông tin."
            )
        description = next(
            (
                block.content
                for block in catalog_product.knowledge_blocks
                if block.kind == "description"
            ),
            "",
        )
        highlights = [
            block.content
            for block in catalog_product.knowledge_blocks
            if block.kind in ("custom", "usage", "campaign")
        ]
        return (
            "Nhiệm vụ stage intro: mở sản phẩm bằng 1 đến 2 câu hoàn chỉnh, tự nhiên, "
            "dí dỏm như MC livestream. Chỉ định vị sản phẩm và tạo tò mò; chưa đọc toàn bộ "
            "giá, khuyến mãi, size, vận chuyển và CTA trong một lượt. "
            f"Tên: {catalog_product.name}. Mô tả: {description}. "
            f"Điểm nổi bật được phép gợi mở: {', '.join(highlights)}."
        )

    def _answer_prompt(self, top: ScoredCluster) -> str:
        cluster = top.cluster
        joined = " | ".join(cluster.members[:5])
        pid = cluster.product_id or "sản phẩm hiện tại"
        topic = cluster.intent or "ý chung"
        return (
            f"Paraphrase ý chung về {topic} từ các comment sau thành một mệnh đề, "
            f"sau đó trả lời grounded về {pid} bằng 1 đến 2 câu ngắn gọn, "
            f"chính xác và nhiệt tình kiểu MC bán hàng; không đọc lại từng comment: {joined}."
        )

    def _grounded_prompt(self, top: ScoredCluster, pid: str, field_name: str, fact: str) -> str:
        """LLM prompt GROUNDED on the O(1) structured value.

        The exact fact comes from the catalog (no hallucination); the LLM only
        rephrases it naturally as a livestream host. This is the
        'fast retrieval + custom phrasing' the user asked for.
        """
        joined = " | ".join(top.cluster.members[:5])
        return (
            f'Khán giả đang hỏi (gom cụm): "{joined}". '
            f'Thông tin chính xác về {pid} ({field_name}): "{fact}". '
            "Dựa ĐÚNG vào thông tin này, trả lời tự nhiên, nhiệt tình, kiểu MC bán hàng "
            "livestream — không bịa thêm số liệu, có thể thêm lời mời chốt đơn."
        )


def _cluster_id_of(by_members: dict[tuple[str, ...], str], item: ScoredCluster) -> str:
    """Map a scored cluster back to its reducer cluster id, if it has one.

    Legacy clusters have no reducer identity: they are absent from the index,
    so the high-value predicate (which is reducer-only) never matches them and
    the relaxed stage gates stay shut for the legacy feed.
    """
    return by_members.get(tuple(item.cluster.member_ids), "")


def _any_high(
    ranked: list[ScoredCluster],
    by_members: dict[tuple[str, ...], str],
    high_value: Callable[[str], bool],
) -> bool:
    return any(high_value(_cluster_id_of(by_members, item)) for item in ranked)


def _stage_is_covered(task, covered: set) -> bool:
    """Whether every key point a stage must say is already covered.

    Conservative by design: a task that declares no key points returns False,
    so an uncovered stage is never skipped on missing evidence. The legacy
    fallback tuples carry no key points and are therefore never skipped.
    """
    if isinstance(task, tuple):
        return False
    points = getattr(task, "key_selling_points", None)
    if points is None and isinstance(task, dict):
        points = task.get("key_selling_points")
    if not points:
        return False
    return all(str(point) in covered for point in points)
