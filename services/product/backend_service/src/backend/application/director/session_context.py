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

from backend.application.entity.models import EntityDocument
from backend.application.render.engines_base import RenderBackend
from .catalog import embedding_text
from .config import StreamConfig
from .decision import Decision, Director
from .embeddings import build_embedder
from .hooks import HookPool
from .state import ProductState, StreamState


@dataclass
class DirectorSession:
    director: Director
    embedder: object
    catalog: list[EntityDocument] = field(default_factory=list)
    base_role: str = ""
    shop_profile: str = ""
    system_prompt: str = ""
    profile_revision: int = 0
    catalog_revision: int = 0
    config_revision: int = 0
    generation_token: str = ""
    runtime_config: dict = field(default_factory=dict)
    accepted_snapshot: dict = field(default_factory=dict)
    t0: float = field(default_factory=time.monotonic)

    def now(self) -> float:
        return time.monotonic() - self.t0


class DirectorRuntime:
    """Per-session Director registry + execution against a RenderBackend."""

    def __init__(self, backend: RenderBackend, embedder: object = None) -> None:
        self.backend = backend
        self._sessions: dict[str, DirectorSession] = {}
        self._embedder = embedder  # process-level shared service; lazy when omitted

    @property
    def embedder(self):
        if self._embedder is None:
            self._embedder = build_embedder()
        return self._embedder

    def _embed(self):
        """Backward-compatible alias for the shared embedding service."""
        return self.embedder

    def get_session(self, session_id: str) -> DirectorSession:
        session = self._sessions.get(session_id)
        if session is None:
            raise KeyError(session_id)
        return session

    def attach(
        self,
        session_id: str,
        products: list[EntityDocument],
        cfg: Optional[StreamConfig] = None,
        hooks: Optional[HookPool] = None,
        shop_profile: Optional[str] = None,
        run_plan: object = None,
        runtime_config: Optional[dict] = None,
    ) -> dict:
        """Atomically accept profile, catalog, plan, and runtime configuration."""
        existing = self._sessions.get(session_id)
        base_cfg = cfg or (existing.director.cfg if existing is not None else StreamConfig())
        runtime_values = dict(runtime_config or {})
        known_fields = set(type(base_cfg).__dataclass_fields__)
        director_values = {
            key: value for key, value in runtime_values.items() if key in known_fields
        }
        candidate_cfg = type(base_cfg)(**{**base_cfg.__dict__, **director_values})
        candidate_cfg.validate_runtime()
        emb = self._embed()
        texts = [embedding_text(p) for p in products]
        vecs = emb.encode(texts) if texts else []
        catalog = {p.id: p for p in products}
        prod_states = [
            ProductState(
                product_id=p.id,
                name=p.name,
                ref_image=(
                    str(p.get_fact("custom.ref_image").value)
                    if p.get_fact("custom.ref_image") is not None
                    else None
                ),
                embedding=list(v),
            )
            for p, v in zip(products, vecs)
        ]
        from backend.config import BASE_SALE_PERSONA, _build_persona

        profile = shop_profile or ""
        opening_lines = [
            f"Chào cả nhà, hôm nay shop có MC đồng hành. Thông tin shop: {profile or 'Livento'}.",
            "Mọi người nhớ like, share, comment và follow để cùng săn deal trong phiên live hôm nay nhé!",
            "Agenda hôm nay gồm: "
            + ", ".join(f"{index + 1}. {product.name}" for index, product in enumerate(products))
            + ".",
        ]
        hook_pool = hooks or HookPool()
        hook_pool.populate("opening", opening_lines)
        if existing is None:
            state = StreamState(products=prod_states, run_plan=run_plan)
            director = Director(
                state=state,
                cfg=candidate_cfg,
                hook_pool=hook_pool,
                catalog=catalog,
            )
            config_revision = int(bool(runtime_values))
            state.cursor.profile_revision = 1
            state.cursor.catalog_revision = 1
            state.cursor.config_revision = config_revision
            session = DirectorSession(
                director=director,
                embedder=emb,
                catalog=list(products),
                base_role=BASE_SALE_PERSONA,
                shop_profile=profile,
                system_prompt=_build_persona(profile),
                profile_revision=1,
                catalog_revision=1,
                config_revision=config_revision,
                generation_token=f"1:1:{config_revision}",
                runtime_config=runtime_values,
            )
        else:
            state = existing.director.state
            old_current_id = state.current_product().product_id if state.current_product() else None
            old_products = {item.product_id: item for item in state.products}
            for item in prod_states:
                old = old_products.get(item.product_id)
                if old is not None:
                    item.status = old.status
                    item.is_introduced = old.is_introduced
                    item.stage = old.stage
                    item.stage_turn_index = old.stage_turn_index
                    item.spoken_turns = old.spoken_turns
                    item.reactive_streak = old.reactive_streak
                    item.cluster_count = old.cluster_count
            state.products = prod_states
            state.run_plan = run_plan
            state.current_product_index = next(
                (
                    index
                    for index, item in enumerate(prod_states)
                    if item.product_id == old_current_id
                ),
                min(state.current_product_index, max(len(prod_states) - 1, 0)),
            )
            state.cursor.product_idx = state.current_product_index
            existing.director.catalog = catalog
            config_changed = candidate_cfg != existing.director.cfg
            existing.director.cfg = candidate_cfg
            existing.director.hooks = hook_pool
            profile_changed = existing.shop_profile != profile
            previous_catalog = [
                item.model_dump() if hasattr(item, "model_dump") else dict(item.__dict__)
                for item in existing.catalog
            ]
            next_catalog = [
                item.model_dump() if hasattr(item, "model_dump") else dict(item.__dict__)
                for item in products
            ]
            for catalog_item in (*previous_catalog, *next_catalog):
                catalog_item.pop("embedding", None)
            catalog_changed = previous_catalog != next_catalog
            existing.profile_revision += int(profile_changed)
            existing.catalog_revision += int(catalog_changed)
            existing.config_revision += int(config_changed)
            existing.generation_token = (
                f"{existing.profile_revision}:{existing.catalog_revision}:"
                f"{existing.config_revision}"
            )
            existing.catalog = list(products)
            existing.shop_profile = profile
            existing.system_prompt = _build_persona(profile)
            existing.runtime_config = {**existing.runtime_config, **runtime_values}
            state.cursor.profile_revision = existing.profile_revision
            state.cursor.catalog_revision = existing.catalog_revision
            state.cursor.config_revision = existing.config_revision
            state.cursor.generation_token += int(
                profile_changed or catalog_changed or config_changed
            )
            if profile_changed or catalog_changed:
                state.answer_variants.clear()
                state.answer_variant_index.clear()
            session = existing
        session.accepted_snapshot = {
            "shop_profile": profile,
            "products": [
                product.model_dump() if hasattr(product, "model_dump") else dict(product.__dict__)
                for product in products
            ],
            "runtime_config": dict(session.runtime_config),
            "profile_revision": session.profile_revision,
            "catalog_revision": session.catalog_revision,
            "config_revision": session.config_revision,
        }
        self._sessions[session_id] = session
        return {
            "attached": session_id,
            "products": [p.id for p in products],
            "embedder": getattr(emb, "name", "?"),
            "profile_revision": session.profile_revision,
            "catalog_revision": session.catalog_revision,
            "config_revision": session.config_revision,
            "generation_token": session.generation_token,
            "accepted_snapshot": session.accepted_snapshot,
        }

    def has(self, session_id: str) -> bool:
        return session_id in self._sessions

    def current_generation_token(self, session_id: str) -> str:
        return self.get_session(session_id).generation_token

    def invalidate_generation(self, session_id: str) -> str:
        """Invalidate queued/prepared work without changing accepted config."""
        session = self.get_session(session_id)
        session.director.state.cursor.generation_token += 1
        session.generation_token = (
            f"{session.profile_revision}:{session.catalog_revision}:"
            f"{session.config_revision}:{session.director.state.cursor.generation_token}"
        )
        return session.generation_token

    def update_runtime_config(self, session_id: str, values: dict) -> dict:
        """Validate and accept scheduling values for the next turn."""
        session = self.get_session(session_id)
        known_fields = set(type(session.director.cfg).__dataclass_fields__)
        director_values = {key: value for key, value in values.items() if key in known_fields}
        candidate = type(session.director.cfg)(
            **{**session.director.cfg.__dict__, **director_values}
        )
        candidate.validate_runtime()
        session.director.cfg = candidate
        session.config_revision += 1
        session.runtime_config = {**session.runtime_config, **values}
        session.generation_token = (
            f"{session.profile_revision}:{session.catalog_revision}:{session.config_revision}"
        )
        session.director.state.cursor.config_revision = session.config_revision
        session.director.state.cursor.generation_token += 1
        session.accepted_snapshot["runtime_config"] = dict(session.runtime_config)
        session.accepted_snapshot["config_revision"] = session.config_revision
        return {
            "config_revision": session.config_revision,
            "generation_token": session.generation_token,
            "runtime_config": dict(session.runtime_config),
        }

    def detach(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def prompt_layers(self, session_id: str, decision: Decision) -> dict[str, str]:
        """Compose prompt via the canonical composer (OpenSpec 1.13).

        Static base/guardrails/decision/fallback files come from the validated
        bundle. Runtime context (shop, product, stage_task) is serialized as
        untrusted data inside explicit begin/end delimiters — it can never
        select, reorder, or replace static files.

        Returns the composed prompt (``final_prompt``) plus diagnostic metadata
        (no rendered prompt text is exposed in events — see coordinator).
        """
        from backend.application.director.prompts.composer import (
            compose_decision_prompt,
            compose_fallback_prompt,
            select_flow,
        )
        from backend.application.director.prompts.loader import load_bundle

        session = self.get_session(session_id)
        stage_task = decision.prompt or decision.text or ""

        # Build context from untrusted runtime data only.
        context: dict[str, str] = {}
        if stage_task:
            context["stage_task"] = stage_task
        if session.shop_profile:
            context["shop_profile"] = session.shop_profile
        # Runtime config and product/comment data are never added to context
        # — they are untrusted content that cannot replace static files.

        bundle = load_bundle()
        has_ctx = bool(stage_task)
        flow = select_flow(has_required_context=has_ctx)
        if flow == "fallback":
            final_prompt = compose_fallback_prompt(bundle=bundle, context=context or None)
        else:
            final_prompt = compose_decision_prompt(bundle=bundle, context=context or None)

        return {
            "base_role": bundle.prompt("base_sales_vi"),
            "shop_profile": session.shop_profile,
            "stage_task": stage_task,
            "final_prompt": final_prompt,
        }
