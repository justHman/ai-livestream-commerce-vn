"""Director runtime — bridges the Director FSM to the RenderBackend say-loop.

One DirectorSession per live session: holds the Director, the embedder, the
structured catalog, and an injected monotonic clock base. The API layer calls:

  attach(session_id, products)        -> build Director + embed catalog (once)
  ingest(session_id, comments, now)   -> Director.decide() -> execute Decision
                                         via backend.say()  -> return what happened

Executing a Decision:
  - speak_hook / answer_fact / close  -> backend.say(text, generate=False)  (TTS only, no LLM)
  - answer_cluster / introduce_product -> backend.say(prompt, generate=True) (LLM->TTS)

This keeps the Director backend-agnostic: it never imports the renderer; the
runtime wires them. Same Director drives cloud or self-host.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from ..render.base import RenderBackend
from .catalog import Product
from .cluster import Comment
from .config import StreamConfig
from .director import Decision, Director
from .embedder import build_embedder
from .hooks import HookPool
from .state import ProductState, StreamState


@dataclass
class DirectorSession:
    director: Director
    embedder: object
    t0: float = field(default_factory=time.monotonic)

    def now(self) -> float:
        return time.monotonic() - self.t0


class DirectorRuntime:
    """Per-session Director registry + execution against a RenderBackend."""

    def __init__(self, backend: RenderBackend) -> None:
        self.backend = backend
        self._sessions: dict[str, DirectorSession] = {}
        self._embedder = None  # shared; lazy

    def _embed(self):
        if self._embedder is None:
            self._embedder = build_embedder()
        return self._embedder

    def attach(
        self,
        session_id: str,
        products: list[Product],
        cfg: Optional[StreamConfig] = None,
        hooks: Optional[HookPool] = None,
    ) -> dict:
        """Build a Director for this session and embed the catalog (once)."""
        emb = self._embed()
        texts = [p.embedding_text() for p in products]
        vecs = emb.encode(texts) if texts else []
        for p, v in zip(products, vecs):
            p.embedding = list(v)

        catalog = {p.id: p for p in products}
        prod_states = [
            ProductState(product_id=p.id, name=p.name, ref_image=p.ref_image,
                         embedding=p.embedding)
            for p in products
        ]
        state = StreamState(products=prod_states)
        director = Director(state=state, cfg=cfg, hook_pool=hooks, catalog=catalog)
        self._sessions[session_id] = DirectorSession(director=director, embedder=emb)
        return {
            "attached": session_id,
            "products": [p.id for p in products],
            "embedder": getattr(emb, "name", "?"),
        }

    def has(self, session_id: str) -> bool:
        return session_id in self._sessions

    def detach(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def ingest(
        self,
        session_id: str,
        raw_comments: list[dict],
        traffic_viewer_count: Optional[int] = None,
        traffic_msg_rate: Optional[float] = None,
    ) -> dict:
        """Embed + feed comments to the Director, execute the Decision.

        raw_comments: [{"text": str, "t": float?}]; missing t uses session clock.
        Returns the Decision + what was spoken.
        """
        ds = self._sessions.get(session_id)
        if ds is None:
            raise KeyError(session_id)

        now = ds.now()
        if traffic_viewer_count is not None:
            ds.director.state.traffic.viewer_count = traffic_viewer_count
        if traffic_msg_rate is not None:
            ds.director.state.traffic.msg_rate = traffic_msg_rate

        texts = [c["text"] for c in raw_comments]
        vecs = ds.embedder.encode(texts) if texts else []
        comments = [
            Comment(text=t, embedding=list(v),
                    t=float(c["t"]) if c.get("t") is not None else now)
            for c, t, v in zip(raw_comments, texts, vecs)
        ]

        decision: Decision = ds.director.decide(comments, now=now)
        spoken = self._execute(session_id, decision)
        return {
            "action": decision.action,
            "spoken": spoken,
            "product_id": decision.product_id,
            "field": decision.field,
            "may_interrupt": decision.may_interrupt,
            "phase": ds.director.state.phase.value,
            "reason": decision.reason,
        }

    def _execute(self, session_id: str, decision: Decision) -> Optional[str]:
        """Turn a Decision into a backend.say() call. Returns spoken text.

        - speak_hook / close          -> verbatim TTS (no LLM), templated lines.
        - answer_fact                 -> LLM GROUNDED on the O(1) structured value
                                         (natural phrasing, exact data). Falls back
                                         to verbatim `text` if no prompt.
        - answer_cluster / introduce  -> LLM generation from the prompt.
        """
        if decision.action in ("speak_hook", "close"):
            if decision.text:
                return self.backend.say(session_id, decision.text, generate=False)
            return None
        if decision.action == "answer_fact":
            if decision.prompt:
                return self.backend.say(session_id, decision.prompt, generate=True)
            if decision.text:
                return self.backend.say(session_id, decision.text, generate=False)
            return None
        if decision.action in ("answer_cluster", "introduce_product"):
            if decision.prompt:
                return self.backend.say(session_id, decision.prompt, generate=True)
            return None
        return None  # idle
