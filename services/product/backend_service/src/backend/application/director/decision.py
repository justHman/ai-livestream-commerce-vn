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
from typing import Optional
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
        decision = self._decide(comments, now)
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

    def _decide(self, comments: list[Comment], now: float) -> Decision:
        s, c = self.state, self.cfg

        # OPENING: three protected grounded turns; comments cannot interrupt.
        self._maybe_leave_opening()
        if s.phase == Phase.OPENING:
            return self._opening_turn()

        if s.phase == Phase.CLOSING:
            if s.closing_spoken:
                return Decision(action="idle", reason="closing already spoken", score=0.0)
            hook = self.hooks.next_hook("closing")
            return Decision(
                action="close", text=hook, stage="closing", reason="closing phase", score=0.0
            )

        # SELLING
        window = [cm for cm in comments if now - cm.t <= c.selection_window_sec]
        clusters = cluster_comments(window, merge_threshold=c.cluster_merge_threshold)
        ranked = [
            item
            for item in rank_clusters(clusters, s, c, now)
            if not item.cluster.member_ids
            or not all(member_id in s.answered_comments for member_id in item.cluster.member_ids)
        ]
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
                return Decision(
                    action="close",
                    text=self.hooks.next_hook("closing"),
                    reason="all products done",
                    score=0.0,
                )
            ranked = [
                item
                for item in rank_clusters(clusters, s, c, now)
                if not any(
                    member_id in s.answered_comments for member_id in item.cluster.member_ids
                )
            ]

        cur = s.current_product()
        actionable_product_ids = [
            comment.product_id
            for comment in window
            if comment.actionable and comment.product_id is not None
        ]
        eligible = []
        if cur is not None:
            for item in ranked:
                if item.cluster.size < 2:
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
                and pivot_product.is_introduced
                and pivot_product.stage_turn_index >= len(self._sales_tasks(pivot_id))
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
                return self._qa_decision(
                    cross_product,
                    cur,
                    pivot=pivot,
                    excursion=not pivot,
                )

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
            ranked = []
        if cur is not None and not cur.is_introduced:
            return Decision(
                action="introduce_product",
                prompt=self._introduce_prompt(cur),
                product_id=cur.product_id,
                stage="intro",
                task_id=f"{cur.product_id}:intro",
                reason="introduce current product before viewer Q&A",
                score=0.0,
            )

        # No viewer question: keep selling the current product one short stage
        # at a time. When its stage plan is exhausted, advance in operator order.
        if not ranked:
            proactive = self._next_sales_turn(cur)
            if proactive is not None:
                return proactive
            self._advance_product()
            if s.phase == Phase.CLOSING:
                return Decision(
                    action="close",
                    text=self.hooks.next_hook("closing"),
                    stage="closing",
                    reason="all product sales stages completed",
                    score=0.0,
                )
            next_product = s.current_product()
            if next_product is not None:
                return Decision(
                    action="introduce_product",
                    prompt=self._introduce_prompt(next_product),
                    product_id=next_product.product_id,
                    stage="intro",
                    task_id=f"{next_product.product_id}:intro",
                    reason="advance to next product after sales stages",
                    score=0.0,
                )
            return Decision(action="idle", reason="no product available", score=0.0)

        # Alternate Q&A with proactive selling so a busy comment stream cannot
        # reduce a product to one intro followed by endless answers.
        if cur is not None and cur.reactive_streak >= 1:
            proactive = self._next_sales_turn(cur)
            if proactive is not None:
                return proactive

        top = ranked[0]
        for skipped in ranked[1:]:
            skipped.cluster.skips += 1
        return self._qa_decision(top, cur)

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
        if decision.stage == "opening":
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
