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

from dataclasses import dataclass
from typing import Optional

from .catalog import Product, route_intent_to_field
from .cluster import Comment, cluster_comments
from .config import StreamConfig
from .hooks import HookPool
from .scorer import ScoredCluster, rank_clusters
from .state import Phase, ProductStatus, StreamState


@dataclass
class Decision:
    """What the Director wants the avatar to do this cycle."""

    action: str                      # "speak_hook" | "answer_cluster" | "answer_fact" | "introduce_product" | "close" | "idle"
    text: Optional[str] = None       # for templated hooks / O(1) factual answers (no LLM)
    prompt: Optional[str] = None     # for LLM-generated answers
    product_id: Optional[str] = None
    field: Optional[str] = None      # structured attribute answered (if action == answer_fact)
    may_interrupt: bool = False
    reason: str = ""


class Director:
    """Orchestrates one live session."""

    def __init__(
        self,
        state: StreamState,
        cfg: Optional[StreamConfig] = None,
        hook_pool: Optional[HookPool] = None,
        catalog: Optional[dict[str, Product]] = None,
    ) -> None:
        self.state = state
        self.cfg = cfg or StreamConfig()
        self.hooks = hook_pool or HookPool()
        # product_id -> structured Product, for O(1) factual answers (TIER 2).
        self.catalog = catalog or {}

    # ── phase transitions ────────────────────────────────────────────

    def _maybe_leave_opening(self) -> None:
        s, c = self.state, self.cfg
        if s.phase != Phase.OPENING:
            return
        if (s.phase_elapsed_sec >= c.opening_timeout_sec
                or s.traffic.viewer_count >= c.viewer_threshold):
            s.phase = Phase.SELLING
            s.phase_elapsed_sec = 0.0
            cur = s.current_product()
            if cur:
                cur.status = ProductStatus.ACTIVE

    def _should_switch_product(self) -> bool:
        """OR of the three justified conditions (challenge 3)."""
        s, c = self.state, self.cfg
        cur = s.current_product()
        if cur is None:
            return False
        return (
            s.product_elapsed_sec >= c.product_time_budget_sec
            or s.sec_since_relevant_msg >= c.engagement_decay_sec
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
        s, c = self.state, self.cfg

        # OPENING: templated hooks (no per-viewer personalization, no LLM)
        self._maybe_leave_opening()
        if s.phase == Phase.OPENING:
            hook = self.hooks.next_hook("opening")
            return Decision(action="speak_hook", text=hook, reason="opening phase")

        if s.phase == Phase.CLOSING:
            hook = self.hooks.next_hook("closing")
            return Decision(action="close", text=hook, reason="closing phase")

        # SELLING
        # product switch check first (challenge 3)
        if self._should_switch_product():
            self._advance_product()
            if s.phase == Phase.CLOSING:
                return Decision(action="close", text=self.hooks.next_hook("closing"),
                                reason="all products done")

        # cluster + rank within selection window
        window = [cm for cm in comments if now - cm.t <= c.selection_window_sec]
        clusters = cluster_comments(window, merge_threshold=c.cluster_merge_threshold)
        ranked = rank_clusters(clusters, s, c, now)

        cur = s.current_product()

        # low traffic OR nothing to answer -> engagement hook (challenge 1)
        if not ranked:
            level = s.traffic.level(c.traffic_low_threshold, c.traffic_high_threshold)
            if level == "low":
                return Decision(action="speak_hook", text=self.hooks.next_hook("engagement"),
                                reason="low traffic, no clusters")
            # medium/high but nothing new yet -> keep presenting current product
            return Decision(
                action="introduce_product",
                prompt=self._introduce_prompt(cur),
                product_id=cur.product_id if cur else None,
                reason="no fresh clusters; continue product",
            )

        top = ranked[0]
        # mark skipped clusters for eventual eviction
        for sc in ranked[1:]:
            sc.cluster.skips += 1

        # bookkeeping for switch conditions
        if cur is not None:
            cur.cluster_count += 1
        s.sec_since_relevant_msg = 0.0

        # going back to a previous product? (challenge 4)
        if top.cluster.product_id and cur and top.cluster.product_id != cur.product_id:
            s.goto_product(top.cluster.product_id)

        may_interrupt = top.score >= c.interrupt_score_threshold

        # TIER 2: O(1) structured-field RETRIEVAL (no per-field embedding, no
        # cosine over fields). The looked-up value GROUNDS an LLM prompt so the
        # answer is natural/varied like a real host — we do NOT read the raw
        # value verbatim. Speed win = skip field-embedding + O(1) lookup; quality
        # win = LLM phrasing on top of exact structured data.
        pid = top.cluster.product_id or (cur.product_id if cur else None)
        field_name = self._route_field(top)
        if field_name and pid in self.catalog:
            fact = self.catalog[pid].answer_field(field_name)
            if fact:
                return Decision(
                    action="answer_fact",
                    prompt=self._grounded_prompt(top, pid, field_name, fact),
                    text=fact,                # raw value kept for logging/fallback
                    product_id=pid,
                    field=field_name,
                    may_interrupt=may_interrupt,
                    reason=f"O(1) field '{field_name}' for {pid} (score={top.score:.2f}) -> LLM phrasing",
                )

        return Decision(
            action="answer_cluster",
            prompt=self._answer_prompt(top),
            product_id=top.cluster.product_id,
            may_interrupt=may_interrupt,
            reason=f"top cluster score={top.score:.2f} (phase={top.phase_rel:.2f}, intent={top.intent:.2f})",
        )

    @staticmethod
    def _route_field(top: ScoredCluster) -> Optional[str]:
        """Map a cluster to a structured attribute field (first member that hits)."""
        for m in top.cluster.members:
            f = route_intent_to_field(m)
            if f:
                return f
        return None

    # ── prompt builders (fed to the LLM) ─────────────────────────────

    def _introduce_prompt(self, product) -> str:
        if product is None:
            return "Giới thiệu sản phẩm hiện tại một cách ngắn gọn, nhiệt tình."
        return (
            f"Giới thiệu sản phẩm '{product.name}' (id {product.product_id}) "
            "ngắn gọn, nhiệt tình, nêu điểm nổi bật và khuyến mãi."
        )

    def _answer_prompt(self, top: ScoredCluster) -> str:
        cluster = top.cluster
        joined = " | ".join(cluster.members[:5])
        pid = cluster.product_id or "sản phẩm hiện tại"
        return (
            f"Khán giả đang hỏi (gom cụm): \"{joined}\". "
            f"Trả lời về {pid} ngắn gọn, chính xác, nhiệt tình kiểu MC bán hàng."
        )

    def _grounded_prompt(self, top: ScoredCluster, pid: str, field_name: str, fact: str) -> str:
        """LLM prompt GROUNDED on the O(1) structured value.

        The exact fact comes from the catalog (no hallucination); the LLM only
        rephrases it naturally as a livestream host. This is the
        'fast retrieval + custom phrasing' the user asked for.
        """
        joined = " | ".join(top.cluster.members[:5])
        return (
            f"Khán giả đang hỏi (gom cụm): \"{joined}\". "
            f"Thông tin chính xác về {pid} ({field_name}): \"{fact}\". "
            "Dựa ĐÚNG vào thông tin này, trả lời tự nhiên, nhiệt tình, kiểu MC bán hàng "
            "livestream — không bịa thêm số liệu, có thể thêm lời mời chốt đơn."
        )
