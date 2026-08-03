"""/api/v1 — stable public API surface.

This router is the production contract. It talks ONLY to the RenderBackend
interface, so it is identical whether the renderer is LiveAvatar cloud or a
future self-host model.

Two planes (see PRODUCTION.md):
  CONTROL (here): JSON + WebSocket — session lifecycle, say, interrupt, events.
  MEDIA (NOT here): avatar VIDEO flows LiveAvatar/renderer -> LiveKit -> browser.

Endpoints:
  GET  /api/v1/health
  POST /api/v1/lite/* and aliases POST /api/v1/sessions/*
  POST /api/v1/avatars CRUD (in-memory)
  WS   /api/v1/ws/control/{session_id}
  WS   /api/v1/ws/platform/{session_id}
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from typing import TYPE_CHECKING, Annotated, Any, Literal, Optional

if TYPE_CHECKING:
    from ..schemas.run_plan import RunPlan

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ..config import AppConfig
from ..llm.base import LLMEngine, _NoopEngine
from ..render.base import FullPipelineBackend, RenderBackend, StartOptions, StreamingAvatarBackend
from ..render.mock import MockRenderBackend
from ..render.orchestrator import StreamOrchestrator
from ..render.queue import BoundedVideoQueue, CoordinatorMetrics
from ..render.locks import SessionLockRegistry
from ..tts.base import TTSEngine, ToneEngine
from .auth import admin_auth, debug_enabled_dep, validate_ws_token, viewer_auth

# Phase B coordinator — optional, constructed in server.py.
# Imported here for type annotations; actual import is safe (no heavy deps).
from ..director.coordinator import DirectorCoordinator

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)
SANDBOX_LAYER_TIMEOUT_SEC = 45.0


# ── Control-plane WS hub (one connection per session) ───────────────


class ControlHub:
    def __init__(self) -> None:
        self._conns: dict[str, WebSocket] = {}

    async def connect(self, session_id: str, ws: WebSocket) -> None:
        await ws.accept()
        self._conns[session_id] = ws

    def disconnect(self, session_id: str) -> None:
        self._conns.pop(session_id, None)

    async def emit(self, session_id: str, event: dict) -> None:
        ws = self._conns.get(session_id)
        if ws is not None:
            try:
                await ws.send_json(event)
            except Exception:
                self.disconnect(session_id)

    async def broadcast(self, event: dict) -> None:
        """Send an event to ALL connected sessions (engine swap notifications)."""
        dead = []
        for sid, ws in self._conns.items():
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(sid)
        for sid in dead:
            self.disconnect(sid)


# ── Request models ──────────────────────────────────────────────────


class StartReq(BaseModel):
    avatar_id: Optional[str] = Field(default=None, max_length=128)
    is_sandbox: bool = True


class SayReq(BaseModel):
    session_id: str = Field(max_length=128)
    text: str = Field(min_length=1, max_length=2_000)
    generate: bool = True


class SessionReq(BaseModel):
    session_id: str = Field(max_length=128)


ProductArrayItem = Annotated[str, Field(min_length=1, max_length=500)]


class ProductIn(BaseModel):
    id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=2_000)
    price: Optional[int] = Field(default=None, ge=0)
    original_price: Optional[int] = Field(default=None, ge=0)
    promotion: Optional[str] = Field(default=None, max_length=500)
    colors: list[ProductArrayItem] = Field(default_factory=list, max_length=32)
    sizes: list[ProductArrayItem] = Field(default_factory=list, max_length=32)
    material: Optional[str] = Field(default=None, max_length=256)
    shipping: Optional[str] = Field(default=None, max_length=500)
    warranty: Optional[str] = Field(default=None, max_length=500)
    in_stock: bool = True
    stock_total: Optional[int] = Field(default=None, ge=0)
    ref_image: Optional[str] = Field(default=None, max_length=2_048)
    features: list[ProductArrayItem] = Field(default_factory=list, max_length=32)

    @model_validator(mode="after")
    def validate_prices(self) -> "ProductIn":
        if (
            self.price is not None
            and self.original_price is not None
            and self.original_price < self.price
        ):
            raise ValueError("original_price must be greater than or equal to price")
        return self


class ShopProfileIn(BaseModel):
    shop_name: str = Field(default="", max_length=256)
    host_name: str = Field(default="", max_length=128)
    address: str = Field(default="", max_length=500)
    phone: str = Field(default="", max_length=64)
    selling_style: str = Field(default="", max_length=1_000)

    def to_persona_text(self) -> str:
        return "\n".join(
            f"{label}: {value}"
            for label, value in (
                ("Tên shop", self.shop_name),
                ("Tên MC", self.host_name),
                ("Địa chỉ", self.address),
                ("Điện thoại", self.phone),
                ("Phong cách bán hàng", self.selling_style),
            )
            if value
        )


class RuntimeConfigReq(BaseModel):
    comment_rate: Optional[float] = Field(default=None, ge=0.2, le=5.0)
    initial_ingest_mode: Optional[str] = Field(default=None, pattern="^(batch|single)$")
    max_qa_clusters_per_window: Optional[int] = Field(default=None, ge=1, le=20)
    qa_window_hard_timeout_sec: Optional[float] = Field(default=None, gt=0, le=600)
    qa_topic_cooldown_sec: Optional[float] = Field(default=None, ge=0, le=3600)
    answer_cache_variants: Optional[int] = Field(default=None, ge=1, le=20)
    prepared_turn_depth: Optional[int] = Field(default=None, ge=1, le=20)
    transient_retry_count: Optional[int] = Field(default=None, ge=0, le=3)
    demand_pivot_enter_share: Optional[float] = Field(default=None, gt=0, le=1)
    demand_pivot_exit_share: Optional[float] = Field(default=None, gt=0, le=1)
    demand_pivot_min_comments: Optional[int] = Field(default=None, ge=1, le=1000)
    demand_pivot_score_margin: Optional[float] = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_pivot_hysteresis(self) -> "RuntimeConfigReq":
        if (
            self.demand_pivot_enter_share is not None
            and self.demand_pivot_exit_share is not None
            and self.demand_pivot_exit_share >= self.demand_pivot_enter_share
        ):
            raise ValueError("pivot shares must satisfy exit < enter")
        return self


class AttachReq(BaseModel):
    session_id: str = Field(max_length=128)
    products: list[ProductIn] = Field(max_length=100)
    shop_profile: Optional[ShopProfileIn | str] = None
    runtime_config: Optional[RuntimeConfigReq] = None

    @model_validator(mode="after")
    def validate_product_ids(self) -> "AttachReq":
        ids = [product.id for product in self.products]
        if len(ids) != len(set(ids)):
            raise ValueError("product ids must be unique")
        return self

    def shop_profile_text(self) -> Optional[str]:
        if isinstance(self.shop_profile, ShopProfileIn):
            return self.shop_profile.to_persona_text() or None
        return self.shop_profile


class CommentIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    t: Optional[float] = None


class IngestReq(BaseModel):
    session_id: str = Field(max_length=128)
    comments: list[CommentIn] = Field(max_length=100)
    viewer_count: Optional[int] = None
    msg_rate: Optional[float] = None


class RuntimeConfigUpdateReq(RuntimeConfigReq):
    session_id: str = Field(max_length=128)


class ChatIn(BaseModel):
    """Wave 2: single chat comment from a viewer (Phase B coordinator path)."""

    session_id: str = Field(max_length=128)
    text: str = Field(min_length=1, max_length=500)
    author: str = Field(min_length=1, max_length=128)
    ts: Optional[float] = None


class TTSPresetIn(BaseModel):
    """Wave 2: select a TTS preset by id (Phase A dropdown)."""

    preset_id: str = Field(min_length=1, max_length=128)


class TTSPreviewReq(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    tts_id: str = Field(min_length=1, max_length=512)
    voice_id: str = Field(default="default", min_length=1, max_length=512)


class AvatarCreateReq(BaseModel):
    scope: Literal["half", "full"] = "half"
    ref_photo_url: Optional[str] = Field(default=None, max_length=2_048)
    voice: Optional[str] = Field(default=None, max_length=128)


class AvatarUpdateReq(BaseModel):
    scope: Optional[Literal["half", "full"]] = None
    ref_photo_url: Optional[str] = Field(default=None, max_length=2_048)
    voice: Optional[str] = Field(default=None, max_length=128)


class SandboxVerifyReq(BaseModel):
    avatar_id: Optional[str] = Field(default=None, max_length=128)
    speech_text: str = Field(
        default="Xin chào, đây là phiên kiểm tra.", min_length=1, max_length=300
    )


class PlanCreateReq(BaseModel):
    """Optional products/persona for deterministic offline run-plan generation."""

    products: list[ProductIn] = Field(default_factory=list, max_length=100)
    persona: Optional[str] = Field(default=None, max_length=1_000)


class PathSayReq(BaseModel):
    text: str = Field(min_length=1, max_length=2_000)
    generate: bool = True


class PathChatIn(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    author: str = Field(default="viewer", min_length=1, max_length=128)
    ts: Optional[float] = None


class PathIngestReq(BaseModel):
    comments: list[CommentIn] = Field(max_length=100)
    viewer_count: Optional[int] = None
    msg_rate: Optional[float] = None


class PathAttachReq(BaseModel):
    products: list[ProductIn] = Field(max_length=100)
    shop_profile: Optional[ShopProfileIn | str] = None
    runtime_config: Optional[RuntimeConfigReq] = None


# ── In-memory avatar store ──────────────────────────────────────────


class AvatarStore:
    """Thread-safe in-memory avatar registry (MVP; no DB)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, dict[str, Any]] = {}

    def create(
        self,
        *,
        scope: str,
        ref_photo_url: Optional[str],
        voice: Optional[str],
    ) -> dict[str, Any]:
        avatar_id = str(uuid.uuid4())
        item = {
            "avatar_id": avatar_id,
            "id": avatar_id,
            "label": f"Custom avatar {avatar_id[:8]}",
            "scope": scope,
            "ref_photo_url": ref_photo_url,
            "thumbnail_url": ref_photo_url,
            "voice": voice,
            "status": "ready",
            "ready": True,
            "capabilities": ["speech", "idle", scope],
        }
        with self._lock:
            self._items[avatar_id] = item
        return dict(item)

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self._items.values()]

    def get(self, avatar_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            item = self._items.get(avatar_id)
            return dict(item) if item is not None else None

    def update(self, avatar_id: str, **fields: Any) -> Optional[dict[str, Any]]:
        with self._lock:
            item = self._items.get(avatar_id)
            if item is None:
                return None
            for k, v in fields.items():
                if v is not None and k in item:
                    item[k] = v
            return dict(item)

    def delete(self, avatar_id: str) -> bool:
        with self._lock:
            return self._items.pop(avatar_id, None) is not None


# ── Wiring (set by core/server.py) ──────────────────────────────────


class V1Deps:
    """Dependencies injected by the server at startup."""

    def __init__(
        self,
        backend: RenderBackend,
        store,
        hub: ControlHub,
        director=None,
        engine_manager=None,
        config: AppConfig | None = None,
        locks: SessionLockRegistry | None = None,
        orchestrators: dict | None = None,
        coordinator: DirectorCoordinator | None = None,
        avatars: AvatarStore | None = None,
        pg_store: Any = None,
        livekit_publishers: Any = None,
    ) -> None:
        self.backend = backend
        self.store = store
        self.hub = hub
        self.director = director  # DirectorRuntime (optional)
        self.engine_manager = engine_manager  # EngineManager (optional, for runtime swap)
        self.config = config  # AppConfig (optional, for auth gates)
        # Task 8: per-session lock registry + active orchestrator map. Lazy
        # defaults so existing tests that build V1Deps directly still work.
        self.locks = locks if locks is not None else SessionLockRegistry()
        # session_id -> {"orchestrator": StreamOrchestrator, "queue": BoundedVideoQueue}
        self.orchestrators: dict = orchestrators if orchestrators is not None else {}
        # Phase B: DirectorCoordinator (optional). When present, /lite/chat
        # pushes comments into the coordinator's ChatQueue, and /lite/attach +
        # /lite/stop also drive the coordinator lifecycle.
        self.coordinator = coordinator
        self.avatars = avatars if avatars is not None else AvatarStore()
        # Optional Postgres runtime store (durable rows). None/disabled when
        # DATABASE_URL is unset. Persistence is fire-and-forget at route sites.
        self.pg_store = pg_store
        self.livekit_publishers = livekit_publishers


_deps: Optional[V1Deps] = None


def init_deps(deps: V1Deps) -> None:
    global _deps
    _deps = deps


def deps() -> V1Deps:
    if _deps is None:
        raise RuntimeError("v1 router not initialized — call init_deps()")
    return _deps


def _request_limit_key(request: Request, scope: str, session_id: str = "") -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{host}:{scope}:{session_id}"


async def _request_session_id(request: Request) -> str:
    session_id = request.path_params.get("session_id", "")
    if session_id:
        return session_id
    try:
        body = await request.json()
    except ValueError:
        return ""
    return str(body.get("session_id", "")) if isinstance(body, dict) else ""


async def rate_limit_viewer(request: Request) -> None:
    session_id = await _request_session_id(request)
    if not request.app.state.api_limiter.allow(_request_limit_key(request, "viewer", session_id)):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


async def rate_limit_admin(request: Request) -> None:
    if not request.app.state.api_limiter.allow(_request_limit_key(request, "admin")):
        raise HTTPException(status_code=429, detail="rate limit exceeded")


def _ws_limit_keys(
    ws: WebSocket, scope: str, session_id: str, connection_id: str
) -> tuple[str, str]:
    host = ws.client.host if ws.client is not None else "unknown"
    session_key = f"{host}:{scope}:{session_id}"
    return f"{session_key}:{connection_id}", session_key


_WS_RATE_LIMIT_CLOSE_CODE = 1008
"""WebSocket policy violation: close when a connection exceeds its message budget."""


async def _allow_ws_message(ws: WebSocket, scope: str, session_id: str, connection_id: str) -> bool:
    connection_key, session_key = _ws_limit_keys(ws, scope, session_id, connection_id)
    allowed = ws.app.state.ws_limiter.allow(connection_key, session_key)
    if not allowed:
        await ws.close(code=_WS_RATE_LIMIT_CLOSE_CODE, reason="message rate limit exceeded")
    return allowed


async def _persist_viewer_msgs(
    d: V1Deps, session_id: str, comments, *, author: str = "viewer"
) -> None:
    """Persist ingested viewer comments to the runtime DB (fire-and-forget).

    No-op when pg_store is None/disabled. Swallows errors so a broken runtime
    DB never breaks the ingest/chat response.
    """
    if d.pg_store is None or not getattr(d.pg_store, "enabled", False):
        return
    for c in comments:
        text = getattr(c, "text", None)
        if not text:
            continue
        try:
            await d.pg_store.insert_viewer_msg(
                session_id,
                text,
                author=author,
                comment_id=None,
                source="platform",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_viewer_msg", session_id
            )


def _mock_or_debug_allowed() -> None:
    """404 /mock/* when not debug_enabled and APP_ENV != dev."""
    cfg = deps().config
    if cfg is None:
        return
    if cfg.debug_enabled or cfg.app_env == "dev":
        return
    raise HTTPException(status_code=404, detail="not found")


def build_run_plan(
    products: list[ProductIn] | list[dict[str, Any]] | None = None,
    persona: Optional[str] = None,
) -> RunPlan:
    """Deterministic offline RunPlan from products (no LLM)."""
    from ..schemas.run_plan import (
        ClosingPhase,
        OpeningPhase,
        ProductSellingPhase,
        RunPlan,
        SellingTask,
    )

    items: list[ProductSellingPhase] = []
    for p in products or []:
        if isinstance(p, ProductIn):
            data = p.model_dump()
        elif hasattr(p, "model_dump"):
            data = p.model_dump()
        else:
            data = dict(p)
        pid = str(data.get("id") or data.get("product_id") or "")
        name = str(data.get("name") or "")
        features = list(data.get("features") or [])
        if not features:
            # Minimal selling points from description / promotion / name.
            features = []
            if data.get("description"):
                features.append(str(data["description"])[:120])
            if data.get("promotion"):
                features.append(f"Khuyến mãi: {data['promotion']}")
            if data.get("price") is not None:
                features.append(f"Giá chỉ {data['price']}")
            if not features:
                features = [f"Ưu điểm nổi bật của {name or pid}"]
        tasks = [
            SellingTask(
                stage="intro",
                task_id=f"{pid}:intro",
                instruction="Mở sản phẩm và tạo tò mò bằng 1 đến 2 câu hoàn chỉnh.",
            )
        ]
        tasks.extend(
            SellingTask(
                stage="benefit",
                task_id=f"{pid}:benefit:{index}",
                instruction=f"Giải thích một lợi ích thực tế từ điểm nổi bật: {feature}",
            )
            for index, feature in enumerate(features)
        )
        if data.get("price") is not None or data.get("promotion"):
            offer = ". ".join(
                part
                for part in (
                    f"Giá bán {data.get('price')} đồng" if data.get("price") is not None else "",
                    f"Giá gốc {data.get('original_price')} đồng"
                    if data.get("original_price") is not None
                    else "",
                    str(data.get("promotion") or ""),
                )
                if part
            )
            tasks.extend(
                (
                    SellingTask(
                        stage="offer",
                        task_id=f"{pid}:offer",
                        instruction=f"Công bố deal rõ ràng, không bỏ dở số tiền: {offer}",
                    ),
                    SellingTask(
                        stage="offer",
                        task_id=f"{pid}:offer-repeat",
                        instruction=(
                            f"Nhấn mạnh lại deal bằng cách diễn đạt khác, có nhịp lặp tự nhiên: {offer}"
                        ),
                    ),
                )
            )
        trust = ". ".join(
            part
            for part in (
                f"Chất liệu {data.get('material')}" if data.get("material") else "",
                f"Màu {', '.join(data.get('colors') or [])}" if data.get("colors") else "",
                f"Size {', '.join(data.get('sizes') or [])}" if data.get("sizes") else "",
                str(data.get("shipping") or ""),
                str(data.get("warranty") or ""),
            )
            if part
        )
        if trust:
            tasks.append(
                SellingTask(
                    stage="trust",
                    task_id=f"{pid}:trust",
                    instruction=f"Tăng tin cậy bằng thông tin chính xác: {trust}",
                )
            )
        tasks.extend(
            (
                SellingTask(
                    stage="cta",
                    task_id=f"{pid}:cta",
                    instruction="Kêu gọi chốt đơn ngay bằng 1 đến 2 câu tự nhiên.",
                ),
                SellingTask(
                    stage="cta",
                    task_id=f"{pid}:cta-urgent",
                    instruction=(
                        "Chèo kéo thêm một lượt, lặp cụm ưu đãi có chủ đích nhưng không lặp nguyên câu."
                    ),
                ),
                SellingTask(
                    stage="transition",
                    task_id=f"{pid}:transition",
                    instruction="Khép sản phẩm hiện tại và chuyển mạch rõ ràng sang sản phẩm tiếp theo.",
                ),
            )
        )
        items.append(
            ProductSellingPhase(
                product_id=pid,
                product_name=name,
                key_selling_points=features,
                tasks=tasks,
            )
        )
    phases: list = ["opening"]
    if items:
        phases.extend(["selling"] * len(items))
    phases.append("closing")
    opening = OpeningPhase()
    if persona:
        opening.persona = persona
    return RunPlan(
        phases=phases,  # type: ignore[arg-type]
        opening=opening,
        selling=items,
        closing=ClosingPhase(),
        persona=persona,
    )


# ── Endpoints ───────────────────────────────────────────────────────


@router.get("/health")
async def health() -> dict[str, Any]:
    from ..config import AppConfig

    cfg = AppConfig.from_env()
    return {
        "ok": True,
        "render_backend": deps().backend.name,
        "store_backend": cfg.store_backend,
        "api_key_loaded": cfg.api_key_present,
        "director_enabled": deps().director is not None,
    }


@router.get("/health/live")
async def health_live() -> dict[str, Any]:
    """Liveness probe — process is alive. Always 200, no deps check."""
    return {"ok": True, "status": "live"}


@router.get("/health/ready")
async def health_ready() -> dict[str, Any]:
    """Readiness probe — backend + engines are ready to serve.

    For the mock backend, readiness = backend exists (the mock renderer can
    always serve frames). For the cloud backend, readiness also requires the
    LLM/TTS engines to be loaded (or that the configured engine is the stub
    "none"/"tone" — in which case nothing is expected to load).

    Finding 2: if a configured REAL engine was requested but FAILED to load
    (``engine_manager.llm_load_error`` / ``tts_load_error`` set), readiness
    is ``False`` and the error is surfaced in the response — even though the
    server fell back to a stub so it could start. A prod deployment with a
    broken GGUF must NOT show "ready" while running echo stubs.
    """
    d = deps()
    backend = d.backend
    backend_name = backend.name if backend is not None else None
    em = d.engine_manager
    llm_engine_name = "none"
    tts_engine_name = "tone"
    llm_loaded = False
    tts_loaded = False
    llm_load_error: Optional[str] = None
    tts_load_error: Optional[str] = None
    if em is not None:
        if em.llm is not None:
            llm_loaded = True
            llm_engine_name = em.llm.name
        else:
            llm_engine_name = em.llm_cfg.get("engine", "none") or "none"
        if em.tts is not None:
            tts_loaded = True
            tts_engine_name = em.tts.name
        else:
            tts_engine_name = em.tts_cfg.get("engine", "tone") or "tone"
        llm_load_error = getattr(em, "llm_load_error", None)
        tts_load_error = getattr(em, "tts_load_error", None)

    if backend is None:
        ready = False
    elif backend_name == "mock":
        # Mock backend can always serve frames. But if a real LLM/TTS engine
        # was configured and FAILED to load (Finding 2), still report
        # not-ready so the operator sees the broken config.
        ready = not (llm_load_error or tts_load_error)
    else:
        # Cloud / self-host: ready if engines are loaded OR the configured
        # engine is the stub (nothing is expected to load). A recorded load
        # failure (Finding 2) overrides → not-ready.
        llm_ok = llm_loaded or llm_engine_name in ("none", "", None)
        tts_ok = tts_loaded or tts_engine_name in ("tone", "", None)
        if llm_load_error:
            llm_ok = False
        if tts_load_error:
            tts_ok = False
        ready = llm_ok and tts_ok

    embedder: Optional[dict] = None
    if d.director is not None:
        from ..director.embedder import embedder_status

        try:
            embedder = embedder_status(d.director.embedder)
        except Exception as exc:
            embedder = {
                "name": "unavailable",
                "mode": "semantic-required",
                "ready": False,
                "degraded": False,
                "error": type(exc).__name__,
            }
        if not embedder["ready"]:
            ready = False
        if embedder["degraded"] and d.config is not None and d.config.app_env != "dev":
            ready = False

    resp: dict[str, Any] = {
        "ok": ready,
        "status": "ready" if ready else "not_ready",
        "render_backend": backend_name,
        "llm_engine": llm_engine_name,
        "tts_engine": tts_engine_name,
    }
    if embedder is not None:
        resp["embedder"] = embedder
    if llm_load_error:
        resp["llm_load_error"] = llm_load_error
    if tts_load_error:
        resp["tts_load_error"] = tts_load_error
    pg = d.pg_store
    if pg is not None and getattr(pg, "enabled", False):
        try:
            pg_ok, pg_error = await pg.health()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Postgres readiness check failed error_type=%s", type(exc).__name__)
            pg_ok, pg_error = False, type(exc).__name__
        resp["postgres"] = "ready" if pg_ok else "not_ready"
        if not pg_ok:
            resp["ok"] = False
            resp["status"] = "not_ready"
            resp["postgres_error"] = pg_error
    return resp


@router.post("/media/livekit/room/{session_id}")
async def media_livekit_room(
    session_id: str,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Mint a LiveKit room-join token for ``session_id`` (room name = session_id).

    Requires LIVEKIT_URL + LIVEKIT_API_KEY + LIVEKIT_API_SECRET. Returns 503 when
    any credential is missing so FE can show a clear "media not configured" state.
    """
    d = deps()
    cfg = d.config or AppConfig.from_env()
    url = (cfg.livekit_url or "").strip()
    api_key = (cfg.livekit_api_key or "").strip()
    api_secret = (cfg.livekit_api_secret or "").strip()
    if not url or not api_key or not api_secret:
        raise HTTPException(
            status_code=503,
            detail="LiveKit not configured (LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET)",
        )
    from ..livekit_tokens import LiveKitConfigError, mint_session_viewer_token

    try:
        token = mint_session_viewer_token(
            api_key=api_key,
            api_secret=api_secret,
            session_id=session_id,
        )
    except (LiveKitConfigError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "livekit_url": url,
        "token": token,
        "room": session_id,
    }


@router.post("/lite/start")
async def lite_start(
    req: StartReq,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    d = deps()
    try:
        result = await asyncio.to_thread(
            d.backend.start,
            StartOptions(avatar_id=req.avatar_id, is_sandbox=req.is_sandbox),
        )
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await d.store.set(result.session_id, {"status": "active", "mode": result.mode})
    if d.livekit_publishers is not None:
        d.livekit_publishers.activate(result.session_id)
    if d.pg_store is not None and getattr(d.pg_store, "enabled", False):
        try:
            await d.pg_store.upsert_session(
                result.session_id,
                status="active",
                mode=result.mode,
                render_backend=d.config.render_backend if d.config else None,
                avatar_id=req.avatar_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=upsert_session", result.session_id
            )
    return result.public_dict()  # frontend-safe only


@router.post("/lite/say")
async def lite_say(
    req: SayReq,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    d = deps()
    # Phase E: streaming coordinator path for StreamingAvatarBackend (mock +
    # future self-host). FullPipelineBackend (cloud) keeps backend.say().
    if isinstance(d.backend, StreamingAvatarBackend):
        return await _streaming_say(d, req)
    if not isinstance(d.backend, FullPipelineBackend):
        raise HTTPException(status_code=501, detail="backend does not support say()")
    # Cloud / FullPipelineBackend path.
    # Per-session lock: 1 say at a time. LLM remote ~6s/call + LiveAvatar
    # sandbox 1 concurrent — overlapping says overload + 503. Reject 409
    # if a turn is already running; FE reuses the queue + retries next tick.
    sid = req.session_id
    if not d.locks.try_acquire(sid):
        raise HTTPException(status_code=409, detail="already_speaking")
    await d.hub.emit(sid, {"type": "avatar.speak_started", "text": req.text})
    try:
        reply = await asyncio.to_thread(d.backend.say, sid, req.text, req.generate)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    except NotImplementedError as exc:
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    finally:
        d.locks.release(sid)
    await d.hub.emit(sid, {"type": "avatar.speak_ended", "reply": reply})
    return {"ok": True, "reply": reply}


async def _streaming_say(d: V1Deps, req: SayReq) -> dict[str, Any]:
    """Run the LLM->chunker->TTS->backend streaming coordinator for one turn.

    Per-session lock: if a turn is already running for this session, reject
    with 409 already_speaking. The lock is released in ``finally`` so a
    subsequent say always succeeds once this one finishes.
    """
    sid = req.session_id
    if not d.locks.try_acquire(sid):
        raise HTTPException(status_code=409, detail="already_speaking")

    # Resolve LLM/TTS engines. Mock mode may have none loaded (e.g. offline
    # defaults LLM_ENGINE=none / TTS_ENGINE=tone) — fall back to the built-in
    # offline stubs so /lite/say always works without a model. When a real
    # engine IS configured in mock mode, server.py loads it and em.llm/.tts
    # are non-None (Finding 1).
    em = d.engine_manager
    llm: LLMEngine = em.llm if (em is not None and em.llm is not None) else _NoopEngine()
    tts: TTSEngine = em.tts if (em is not None and em.tts is not None) else ToneEngine()
    # Note: if a real engine was configured but FAILED to load, em.llm/.tts
    # are None and we fall back to the stubs here so /lite/say still responds.
    # /health/ready is the honest signal that the configured engine is broken
    # (Finding 2); this path keeps the server functional for dev/demo.

    # Bounded queue + metrics for this utterance.
    cfg = d.config or AppConfig()
    max_q = getattr(cfg, "avatar_max_queue_windows", 5)
    queue = BoundedVideoQueue(max_size=max_q)
    metrics = CoordinatorMetrics()
    orch_cfg = {
        "text_chunk_min_chars": getattr(cfg, "text_chunk_min_chars", 12),
        "text_chunk_target_chars": getattr(cfg, "text_chunk_target_chars", 40),
        "text_chunk_max_chars": getattr(cfg, "text_chunk_max_chars", 80),
        "text_chunk_flush_timeout_ms": getattr(cfg, "text_chunk_flush_timeout_ms", 350),
    }
    # Widen the try/finally to wrap the speak_started emit, orchestrator
    # construction, run, drain, and return. If the emit raises (e.g. WS
    # error), the finally still releases the per-session lock and cleans up
    # the orchestrator entry — otherwise the session would be permanently
    # locked (every future /lite/say -> 409) and the entry would leak.
    try:
        orchestrator = StreamOrchestrator(
            llm=llm,
            tts=tts,
            backend=d.backend,
            queue=queue,
            metrics=metrics,
            config=orch_cfg,
            audio_window_callback=d.livekit_publishers.publish
            if d.livekit_publishers is not None
            else None,
        )
        # Register the orchestrator so /lite/interrupt can cancel it.
        d.orchestrators[sid] = {"orchestrator": orchestrator, "queue": queue}

        await d.hub.emit(sid, {"type": "avatar.speak_started", "text": req.text})
        if req.generate:
            system_prompt = None
            if em is not None and hasattr(em, "_system_prompt"):
                system_prompt = em._system_prompt or None
            spoken = await orchestrator.run(sid, req.text, system_prompt=system_prompt)
        else:
            spoken = await orchestrator.speak_verbatim(sid, req.text)
        # Drain the queue: emit one WS event per VideoWindow to the control
        # hub so connected clients see frame updates. In production the MEDIA
        # plane carries the actual video; this control event is for telemetry.
        windows_emitted = 0
        while queue.qsize() > 0:
            vw = await queue.get()
            windows_emitted += 1
            await d.hub.emit(
                sid,
                {
                    "type": "avatar.video_window",
                    "seq": vw.seq,
                    "is_final": vw.is_final,
                    "duration_ms": vw.duration_ms,
                },
            )
        await d.hub.emit(sid, {"type": "avatar.speak_ended", "reply": spoken})
        return {
            "ok": True,
            "reply": spoken,
            "windows": windows_emitted,
            "metrics": metrics.to_dict(),
        }
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    finally:
        d.orchestrators.pop(sid, None)
        d.locks.release(sid)


@router.post("/lite/interrupt")
async def lite_interrupt(req: SessionReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    d = deps()
    # Task 8: if there is an active streaming orchestrator for this session,
    # cancel it first (stops emission + drains the bounded queue).
    try:
        if d.coordinator is not None and d.coordinator.has(req.session_id):
            await d.coordinator.interrupt(req.session_id)
        else:
            entry = d.orchestrators.get(req.session_id)
            if entry is not None:
                orch: StreamOrchestrator = entry["orchestrator"]
                await orch.cancel(req.session_id)
            await asyncio.to_thread(d.backend.interrupt, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await d.hub.emit(req.session_id, {"type": "avatar.interrupted"})
    return {"ok": True}


@router.post("/lite/stop")
async def lite_stop(req: SessionReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    d = deps()
    # Wave 2: stop the DirectorCoordinator for this session (before teardown).
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        d.coordinator.stop(req.session_id)
    entry = d.orchestrators.get(req.session_id)
    if entry is not None:
        orchestrator: StreamOrchestrator = entry["orchestrator"]
        await orchestrator.cancel(req.session_id)
    try:
        await asyncio.to_thread(d.backend.stop, req.session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if d.livekit_publishers is not None:
        await d.livekit_publishers.stop(req.session_id)
    if d.director is not None:
        d.director.detach(req.session_id)
    await d.store.delete(req.session_id)
    await d.hub.emit(req.session_id, {"type": "session.stopped"})
    # P4 hardening: drop the per-session lock entry to prevent memory leak.
    d.locks.drop(req.session_id)
    return {"ok": True, "stopped": req.session_id}


# ── Director-driven endpoints (orchestration) ───────────────────────


@router.post("/lite/attach")
async def lite_attach(req: AttachReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    """Attach a Director to a started session: build the FSM + embed the catalog."""
    d = deps()
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    from ..director.catalog import Product

    if await d.store.get(req.session_id) is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    products = [Product(**p.model_dump()) for p in req.products]
    shop_profile = req.shop_profile_text()
    # Re-attach updates the existing runtime/coordinator atomically. Stopping
    # the coordinator here would erase the active checkpoint and rolling window.
    has_coordinator = d.coordinator is not None and d.coordinator.has(req.session_id)
    try:
        runtime_values = (
            req.runtime_config.model_dump(exclude_none=True)
            if req.runtime_config is not None
            else None
        )
        info = await asyncio.to_thread(
            d.director.attach,
            req.session_id,
            products,
            shop_profile=shop_profile,
            run_plan=build_run_plan(req.products, persona=shop_profile),
            runtime_config=runtime_values,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # M3: freeze the product snapshot into the runtime DB (fire-and-forget).
    if d.pg_store is not None and getattr(d.pg_store, "enabled", False):
        try:
            await d.pg_store.insert_product_snapshot(
                req.session_id, [p.model_dump() for p in req.products]
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Postgres persistence failed session=%s operation=insert_product_snapshot",
                req.session_id,
            )
    # Set the persona before the Coordinator tick exists. Attach is config-only;
    # the Coordinator stays dormant until the first viewer comment is ingested.
    if shop_profile and hasattr(d.backend, "set_persona"):
        try:
            d.backend.set_persona(req.session_id, shop_profile)
        except Exception:
            logger.warning(
                "set_persona failed session=%s (continuing with default persona)",
                req.session_id,
            )
    if d.coordinator is not None:
        if not has_coordinator:
            d.coordinator.start(
                session_id=req.session_id,
                products=products,
                activated=False,
            )
        else:
            d.coordinator.update_catalog(req.session_id, products)
    return {"ok": True, "will_speak": False, **info}


@router.patch("/lite/config")
async def lite_config_update(
    req: RuntimeConfigUpdateReq,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    d = deps()
    values = req.model_dump(exclude={"session_id"}, exclude_none=True)
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        updater = d.coordinator.update_runtime_config
    elif d.director is not None and d.director.has(req.session_id):
        updater = d.director.update_runtime_config
    else:
        raise HTTPException(status_code=409, detail="session not attached")
    try:
        return {"ok": True, "session_id": req.session_id, **updater(req.session_id, values)}
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/lite/ingest")
async def lite_ingest(
    req: IngestReq,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    """Feed viewer comments to the Director; it decides + the avatar speaks.

    This is the closed loop: comments -> cluster/score -> Decision ->
    background streaming pipeline. Frontend just POSTs raw comments; the avatar reacts.

    Wave 2: when a DirectorCoordinator is active for this session, route
    comments through it (async ChatQueue path) instead of the sync Director
    ingest. The coordinator's tick loop will decide and speak asynchronously.
    Falls back to the existing sync Director path when coordinator is None.
    """
    d = deps()
    # Wave 2: coordinator path (async tick loop drains the queue).
    if d.coordinator is not None and d.coordinator.has(req.session_id):
        d.coordinator.update_traffic(
            req.session_id,
            viewer_count=req.viewer_count,
            msg_rate=req.msg_rate,
        )
        for c in req.comments:
            d.coordinator.ingest(req.session_id, c.text, author="viewer", ts=c.t)
        await _persist_viewer_msgs(d, req.session_id, req.comments, author="viewer")
        return {"ok": True, "accepted": True, "queue_stats": d.coordinator.stats(req.session_id)}

    # Fallback: original sync Director path.
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    if not d.director.has(req.session_id):
        raise HTTPException(status_code=409, detail="call /lite/attach first")

    raw = [c.model_dump() for c in req.comments]
    await d.hub.emit(req.session_id, {"type": "director.cycle_started"})
    try:
        result = await asyncio.to_thread(
            d.director.ingest, req.session_id, raw, req.viewer_count, req.msg_rate
        )
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    await _persist_viewer_msgs(d, req.session_id, req.comments, author="viewer")
    await d.hub.emit(req.session_id, {"type": "director.spoke", **result})
    return {"ok": True, **result}


# ── Wave 2: single-comment chat endpoint (Phase B coordinator) ────────


@router.post("/lite/chat", status_code=202)
async def lite_chat(
    payload: ChatIn,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    """Accept a single viewer chat comment via the DirectorCoordinator.

    Returns 202 Accepted immediately; the coordinator's tick loop processes
    comments asynchronously. Returns 404 if the coordinator is not active or
    the session is not attached.
    """
    d = deps()
    if d.coordinator is None or not d.coordinator.has(payload.session_id):
        raise HTTPException(404, "session not attached to coordinator")
    comment = d.coordinator.ingest(
        session_id=payload.session_id,
        text=payload.text,
        author=payload.author,
        ts=payload.ts,
    )
    await _persist_viewer_msgs(
        d,
        payload.session_id,
        [CommentIn(text=payload.text, t=payload.ts)],
        author=payload.author,
    )
    return {
        "accepted": True,
        "comment_id": comment.id,
        "queue_stats": d.coordinator.stats(payload.session_id),
    }


# ── Engine management endpoints (runtime LLM/TTS swap) ───────────────


class EngineSwapReq(BaseModel):
    engine: str
    model: str = ""
    model_path: str = ""
    device: str = "auto"
    # LLM-specific
    n_ctx: int = 4096
    n_gpu_layers: int = -1
    max_model_len: int = 4096
    max_tokens: int = 128
    temperature: float = 0.7
    quantization: Optional[str] = None
    # TTS-specific
    sample_rate: int = 24000
    ref_audio: Optional[str] = None
    # Extra passthrough
    extra: dict[str, Any] = {}


@router.get("/engines")
async def engines_status(_: None = Depends(admin_auth)) -> dict[str, Any]:
    """List available LLM/TTS presets + currently loaded engines."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    return d.engine_manager.status()


@router.post("/engines/llm")
async def swap_llm(
    req: EngineSwapReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Swap the LLM engine at runtime. Unloads the old model (frees VRAM),
    loads the new one, re-configures the cloud RenderBackend."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    cfg = {
        "engine": req.engine,
        "model": req.model,
        "model_path": req.model_path,
        "device": req.device,
        "n_ctx": req.n_ctx,
        "n_gpu_layers": req.n_gpu_layers,
        "max_model_len": req.max_model_len,
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "quantization": req.quantization,
    }
    cfg.update(req.extra)
    await d.hub.broadcast(
        {"type": "engine.llm_swap_started", "engine": req.engine, "model": req.model}
    )
    try:
        info = await asyncio.to_thread(d.engine_manager.load_llm, cfg)
        d.engine_manager.reconfigure_cloud()
    except Exception as exc:
        await d.hub.broadcast({"type": "engine.llm_swap_failed", "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await d.hub.broadcast(
        {"type": "engine.llm_swapped", "engine": info.engine, "model": info.model}
    )
    return {"ok": True, "engine": info.engine, "model": info.model, "name": info.name}


@router.post("/engines/tts")
async def swap_tts(
    req: EngineSwapReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Swap the TTS engine at runtime. Unloads the old model (frees VRAM),
    loads the new one, re-configures the cloud RenderBackend."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=501, detail="Engine manager not enabled")
    cfg = {
        "engine": req.engine,
        "model": req.model,
        "weights_path": req.model or req.model_path,
        "device": req.device,
        "sample_rate": req.sample_rate,
        "ref_audio": req.ref_audio,
    }
    cfg.update(req.extra)
    await d.hub.broadcast(
        {"type": "engine.tts_swap_started", "engine": req.engine, "model": req.model}
    )
    try:
        info = await asyncio.to_thread(d.engine_manager.load_tts, cfg)
        d.engine_manager.reconfigure_cloud()
    except Exception as exc:
        await d.hub.broadcast({"type": "engine.tts_swap_failed", "error": str(exc)})
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    await d.hub.broadcast(
        {
            "type": "engine.tts_swapped",
            "engine": info.engine,
            "model": info.model,
            "sample_rate": info.sample_rate,
        }
    )
    return {
        "ok": True,
        "engine": info.engine,
        "model": info.model,
        "name": info.name,
        "sample_rate": info.sample_rate,
    }


@router.post("/engines/tts/preset")
async def set_tts_preset(
    payload: TTSPresetIn,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Select a TTS preset by id (Phase A dropdown). Updates the EngineManager's
    in-memory TTS config without loading the model. The next ``POST /engines/tts``
    or full reload will apply it."""
    d = deps()
    if d.engine_manager is None:
        raise HTTPException(status_code=503, detail="engine manager not ready")
    try:
        updated = d.engine_manager.apply_tts_preset(payload.preset_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown preset {payload.preset_id}")
    return {"preset_id": payload.preset_id, "tts_cfg": updated}


@router.post("/engines/tts/preview")
async def preview_tts(
    payload: TTSPreviewReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> Response:
    """Synthesize bounded browser-playable WAV without creating an avatar session."""
    import io
    import wave

    d = deps()
    manager = d.engine_manager
    if manager is None or manager.tts is None:
        raise HTTPException(status_code=503, detail="TTS engine not loaded")
    try:
        manager.validate_tts_selection(payload.tts_id, payload.voice_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    from ..tts.base import TTSRequest

    try:
        audio = await asyncio.wait_for(
            asyncio.to_thread(
                manager.tts.synthesize,
                TTSRequest(text=payload.text, voice=payload.voice_id),
            ),
            timeout=30.0,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="TTS preview timed out") from exc
    except Exception as exc:
        logger.warning("TTS preview failed error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="TTS preview failed") from exc

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(audio.sample_rate)
        wav.writeframes(audio.to_pcm16_bytes())
    return Response(
        content=output.getvalue(),
        media_type="audio/wav",
        headers={
            "X-TTS-Id": payload.tts_id,
            "X-Voice-Id": payload.voice_id,
            "X-Sample-Rate": str(audio.sample_rate),
        },
    )


# ── Debug mode endpoints (mock viewer traffic + products) ─────────────


class DebugStartReq(BaseModel):
    session_id: str = Field(max_length=128)
    interval_sec: float = 5.0  # how often to feed mock comments
    traffic_mode: str = Field(default="random", max_length=32)


@router.post("/debug/start")
async def debug_start(
    req: DebugStartReq,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Start debug mode: feed mock viewer comments + simulated traffic to the Director."""
    d = deps()
    if d.director is None:
        raise HTTPException(status_code=501, detail="Director not enabled")
    if not d.director.has(req.session_id):
        raise HTTPException(status_code=409, detail="call /lite/attach first")
    from ..debug.traffic_sim import TrafficSimulator

    sim = TrafficSimulator(
        director=d.director,
        hub=d.hub,
        session_id=req.session_id,
        interval_sec=req.interval_sec,
        mode=req.traffic_mode,
    )
    sim.start()
    # Store the sim so we can stop it later
    if not hasattr(deps(), "_debug_sims"):
        d._debug_sims = {}
    d._debug_sims[req.session_id] = sim
    await d.hub.emit(
        req.session_id,
        {"type": "debug.started", "mode": req.traffic_mode, "interval_sec": req.interval_sec},
    )
    return {
        "ok": True,
        "session_id": req.session_id,
        "mode": req.traffic_mode,
        "interval_sec": req.interval_sec,
    }


class DebugStopReq(BaseModel):
    session_id: str = Field(max_length=128)


@router.post("/debug/stop")
async def debug_stop(
    req: DebugStopReq,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Stop debug mode: stop the mock traffic simulator."""
    d = deps()
    sim = getattr(d, "_debug_sims", {}).pop(req.session_id, None)
    if sim is not None:
        sim.stop()
        await d.hub.emit(req.session_id, {"type": "debug.stopped"})
        return {"ok": True, "stopped": req.session_id}
    return {"ok": False, "detail": "no debug session running"}


@router.get("/debug/status/{session_id}")
async def debug_status(
    session_id: str,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Check if debug mode is running for a session."""
    d = deps()
    sim = getattr(d, "_debug_sims", {}).get(session_id)
    if sim is not None:
        return {
            "running": True,
            "mode": sim.mode,
            "interval_sec": sim.interval_sec,
            "msgs_sent": sim.msgs_sent,
            "cycles": sim.cycles,
        }
    return {"running": False}


@router.get("/debug/mock_products")
async def debug_mock_products(
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Return a mock product catalog for debug/testing."""
    from ..debug.mock_data import MOCK_PRODUCTS

    return {"products": [p for p in MOCK_PRODUCTS]}


@router.get("/debug/mock_viewer_msgs")
async def debug_mock_viewer_msgs(
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Return the pool of mock viewer messages for debug."""
    from ..debug.mock_data import MOCK_VIEWER_MSGS

    return {"count": len(MOCK_VIEWER_MSGS), "messages": MOCK_VIEWER_MSGS}


@router.get("/debug/clusters/{session_id}")
async def debug_clusters(
    session_id: str,
    _dbg: None = Depends(debug_enabled_dep),
    _adm: None = Depends(admin_auth),
) -> dict[str, Any]:
    """Re-cluster the session's rolling comments + return current clusters.

    Stage 2 visibility: the coordinator queue auto-reactive path needs a
    self-host renderer (Stage 3), so this endpoint lets the FE show the
    cluster state (gom cụm) without relying on coordinator auto-speak.
    """
    coordinator = deps().coordinator
    if coordinator is None or not coordinator.has(session_id):
        raise HTTPException(404, "session not attached to Director coordinator")
    try:
        snapshot = coordinator.cluster_snapshot(session_id)
    except KeyError as exc:
        raise HTTPException(404, "session not attached") from exc
    return {**snapshot, "queue_stats": coordinator.stats(session_id)}


@router.websocket("/ws/control/{session_id}")
async def ws_control(ws: WebSocket, session_id: str) -> None:
    d = deps()
    # Task 7: validate token BEFORE accept(). On invalid, close with 4401
    # and return without accepting (no control.connected event leaks).
    cfg = d.config
    if cfg is not None and not validate_ws_token(ws, cfg):
        await ws.close(code=4401)
        return
    await d.hub.connect(session_id, ws)
    connection_id = str(uuid.uuid4())
    await ws.send_json({"type": "control.connected", "session_id": session_id})
    try:
        while True:
            msg = await ws.receive_json()
            if not await _allow_ws_message(ws, "viewer", session_id, connection_id):
                return
            mtype = msg.get("type")
            if mtype == "interrupt":
                try:
                    # Task 8: cancel any active streaming orchestrator first.
                    entry = d.orchestrators.get(session_id)
                    if entry is not None:
                        orch: StreamOrchestrator = entry["orchestrator"]
                        await orch.cancel(session_id)
                    await asyncio.to_thread(d.backend.interrupt, session_id)
                    await d.hub.emit(session_id, {"type": "avatar.interrupted"})
                except KeyError:
                    await ws.send_json({"type": "error", "detail": "unknown session_id"})
            elif mtype == "ping":
                await ws.send_json({"type": "pong"})
    except WebSocketDisconnect:
        d.hub.disconnect(session_id)


# ── Mock media endpoints (only when RENDER_BACKEND=mock) ────────────

_MJPEG_BOUNDARY = "mockmjpegboundary"


def _mock_backend() -> Optional[MockRenderBackend]:
    """Return the mock backend if the active backend is a MockRenderBackend.

    Returns None otherwise so route handlers can 404 cleanly.
    """
    backend = deps().backend
    if isinstance(backend, MockRenderBackend):
        return backend
    return None


@router.get("/mock/frame/{session_id}.png")
async def mock_frame_png(
    session_id: str,
    _: None = Depends(_mock_or_debug_allowed),
) -> Response:
    """Return the latest rendered frame as a PNG.

    Only available when the active backend is MockRenderBackend.
    """
    mb = _mock_backend()
    if mb is None:
        raise HTTPException(status_code=404, detail="mock backend not active")
    try:
        png = await asyncio.to_thread(mb.get_last_frame_png, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return Response(content=png, media_type="image/png")


@router.get("/mock/video/{session_id}.mjpeg")
async def mock_video_mjpeg(
    session_id: str,
    _: None = Depends(_mock_or_debug_allowed),
) -> StreamingResponse:
    """Continuous MJPEG stream (Wave 2 rewrite).

    Emits idle-loop frames at ~25 fps when no utterance is playing. When
    a ``BoundedVideoQueue`` is active for the session (an utterance is
    running through ``_streaming_say`` or the coordinator), utterance frames
    are served from the queue and fall back to idle on underflow.

    The stream runs until the client disconnects or the session is stopped.
    Only available when the active backend is MockRenderBackend.
    """
    import time as _time

    mb = _mock_backend()
    if mb is None:
        raise HTTPException(status_code=404, detail="mock backend not active")
    # Validate session exists.
    if session_id not in mb._sessions:
        raise HTTPException(status_code=404, detail="unknown session_id")

    d = deps()
    fps = mb.fps
    frame_interval_s = 1.0 / fps

    async def _gen():
        boundary = _MJPEG_BOUNDARY
        while True:
            # Check whether a streaming orchestrator queue exists for this
            # session (active utterance). If so, use get_or_idle for frame
            # pacing. Otherwise serve idle frames directly.
            entry = d.orchestrators.get(session_id)
            if entry is not None:
                queue: BoundedVideoQueue = entry["queue"]

                def _idle_fn() -> bytes:
                    return mb.get_idle_frame_jpeg(
                        session_id, int(_time.monotonic_ns() // 1_000_000)
                    )

                try:
                    jpeg, _is_idle = await queue.get_or_idle(
                        _idle_fn, timeout_ms=int(frame_interval_s * 1000)
                    )
                except (KeyError, asyncio.CancelledError):
                    break
            else:
                # Pure idle: serve pre-rendered idle frames at frame rate.
                try:
                    jpeg = mb.get_idle_frame_jpeg(
                        session_id, int(_time.monotonic_ns() // 1_000_000)
                    )
                except KeyError:
                    break
                await asyncio.sleep(frame_interval_s)

            header = (
                f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpeg)}\r\n\r\n"
            ).encode("ascii")
            yield header + jpeg + b"\r\n"

    return StreamingResponse(
        _gen(),
        media_type=f"multipart/x-mixed-replace; boundary={_MJPEG_BOUNDARY}",
    )


@router.get("/mock/status/{session_id}")
async def mock_status(
    session_id: str,
    _: None = Depends(_mock_or_debug_allowed),
) -> dict[str, Any]:
    """Return the current status of a mock render session as JSON."""
    mb = _mock_backend()
    if mb is None:
        raise HTTPException(status_code=404, detail="mock backend not active")
    try:
        status = await asyncio.to_thread(mb.session_status, session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="unknown session_id")
    return {"session_id": session_id, "status": status, "backend": "mock"}


# ── Sessions aliases (compat keep /lite/*) ──────────────────────────


@router.post("/sessions")
async def sessions_start(
    req: StartReq,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_start(req, _)


@router.post("/sessions/{session_id}/say")
async def sessions_say(
    session_id: str,
    req: PathSayReq,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_say(SayReq(session_id=session_id, text=req.text, generate=req.generate), _)


@router.post("/sessions/{session_id}/interrupt")
async def sessions_interrupt(session_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    return await lite_interrupt(SessionReq(session_id=session_id), _)


@router.post("/sessions/{session_id}/stop")
async def sessions_stop(session_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    return await lite_stop(SessionReq(session_id=session_id), _)


@router.post("/sessions/{session_id}/attach")
async def sessions_attach(
    session_id: str, req: PathAttachReq, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    return await lite_attach(
        AttachReq(
            session_id=session_id,
            products=req.products,
            shop_profile=req.shop_profile,
            runtime_config=req.runtime_config,
        ),
        _,
    )


@router.post("/sessions/{session_id}/ingest")
async def sessions_ingest(
    session_id: str,
    req: PathIngestReq,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_ingest(
        IngestReq(
            session_id=session_id,
            comments=req.comments,
            viewer_count=req.viewer_count,
            msg_rate=req.msg_rate,
        ),
        _,
    )


@router.post("/sessions/{session_id}/chat", status_code=202)
async def sessions_chat(
    session_id: str,
    req: PathChatIn,
    _: None = Depends(viewer_auth),
    _limit: None = Depends(rate_limit_viewer),
) -> dict[str, Any]:
    return await lite_chat(
        ChatIn(
            session_id=session_id,
            text=req.text,
            author=req.author,
            ts=req.ts,
        ),
        _,
    )


@router.post("/sessions/{session_id}/plan/create")
async def sessions_plan_create(
    session_id: str,
    req: PlanCreateReq | None = None,
    _: None = Depends(viewer_auth),
) -> dict[str, Any]:
    """Generate a minimal deterministic RunPlan and store on the session."""
    d = deps()
    meta = await d.store.get(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    body = req or PlanCreateReq()
    plan = build_run_plan(body.products, persona=body.persona)
    plan_dict = plan.model_dump()
    meta = dict(meta)
    meta["run_plan"] = plan_dict
    await d.store.set(session_id, meta)

    # If director runtime has the session, attach plan + reset cursor.
    if d.director is not None and d.director.has(session_id):
        try:
            ds = d.director._sessions.get(session_id)
            if ds is not None:
                state = ds.director.state
                state.run_plan = plan
                state.cursor.phase = "opening"
                state.cursor.product_idx = 0
                state.cursor.talking_point_idx = 0
                state.covered_points = {}
        except Exception:
            pass
    return {"ok": True, "session_id": session_id, "plan": plan_dict}


# ── Avatars CRUD (in-memory) ────────────────────────────────────────


@router.post("/avatars")
async def avatars_create(req: AvatarCreateReq, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    return deps().avatars.create(
        scope=req.scope,
        ref_photo_url=req.ref_photo_url,
        voice=req.voice,
    )


@router.get("/avatars")
async def avatars_list(_: None = Depends(viewer_auth)) -> dict[str, Any]:
    return {"avatars": deps().avatars.list()}


@router.get("/avatars/{avatar_id}")
async def avatars_get(avatar_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    item = deps().avatars.get(avatar_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return item


@router.put("/avatars/{avatar_id}")
async def avatars_put(
    avatar_id: str, req: AvatarUpdateReq, _: None = Depends(viewer_auth)
) -> dict[str, Any]:
    item = deps().avatars.update(
        avatar_id,
        scope=req.scope,
        ref_photo_url=req.ref_photo_url,
        voice=req.voice,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return item


@router.delete("/avatars/{avatar_id}")
async def avatars_delete(avatar_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    ok = deps().avatars.delete(avatar_id)
    if not ok:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    return {"ok": True, "deleted": avatar_id}


@router.post("/avatars/{avatar_id}/idle/regenerate")
async def avatars_idle_regenerate(avatar_id: str, _: None = Depends(viewer_auth)) -> dict[str, Any]:
    item = deps().avatars.get(avatar_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown avatar_id")
    # Stub: real idle pre-render is avatar-server work.
    return {"ok": True, "avatar_id": avatar_id, "status": "ready", "frames": 75}


# ── Platform WS (viewer chat ingress) ───────────────────────────────


@router.websocket("/ws/platform/{session_id}")
async def ws_platform(ws: WebSocket, session_id: str) -> None:
    """Accept platform chat JSON {text, author?} → coordinator ChatQueue."""
    d = deps()
    cfg = d.config
    if cfg is not None and not validate_ws_token(ws, cfg):
        await ws.close(code=4401)
        return
    await ws.accept()
    connection_id = str(uuid.uuid4())
    await ws.send_json({"type": "platform.connected", "session_id": session_id})
    try:
        while True:
            msg = await ws.receive_json()
            if not await _allow_ws_message(ws, "viewer", session_id, connection_id):
                return
            text = msg.get("text")
            if not isinstance(text, str) or not (text := text.strip()):
                await ws.send_json({"type": "error", "detail": "text required"})
                continue
            if len(text) > 500:
                await ws.send_json({"type": "error", "detail": "text too long"})
                continue
            author = msg.get("author") or "viewer"
            if not isinstance(author, str) or not author or len(author) > 128:
                await ws.send_json({"type": "error", "detail": "invalid author"})
                continue
            ts = msg.get("ts")
            if d.coordinator is not None and d.coordinator.has(session_id):
                try:
                    comment = d.coordinator.ingest(session_id, text, author=author, ts=ts)
                    await ws.send_json(
                        {
                            "type": "platform.accepted",
                            "comment_id": comment.id,
                        }
                    )
                except KeyError:
                    await ws.send_json({"type": "error", "detail": "session not attached"})
            else:
                # Store pending message on session meta when coordinator absent.
                meta = await d.store.get(session_id) or {}
                pending = list(meta.get("pending_platform_chat") or [])
                pending.append({"text": text, "author": author, "ts": ts})
                meta["pending_platform_chat"] = pending[-100:]
                await d.store.set(session_id, meta)
                await ws.send_json({"type": "platform.stored", "pending": len(pending)})
    except WebSocketDisconnect:
        return


# ── Admin ───────────────────────────────────────────────────────────


@router.post("/admin/sandbox/verify")
async def verify_sandbox(
    payload: SandboxVerifyReq,
    _: None = Depends(admin_auth),
    _limit: None = Depends(rate_limit_admin),
) -> dict[str, Any]:
    """Run bounded verification and clean late provider results."""
    backend = deps().backend
    layers: list[dict[str, Any]] = []
    session_id: Optional[str] = None

    async def run_layer(name: str, operation, error: str):
        started = time.monotonic()
        worker = asyncio.create_task(asyncio.to_thread(operation))
        try:
            result = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=SANDBOX_LAYER_TIMEOUT_SEC,
            )
        except asyncio.CancelledError:
            worker.cancel()
            raise
        except Exception:
            logger.warning("Sandbox verification failed layer=%s", name)
            layers.append(
                {
                    "name": name,
                    "status": "fail",
                    "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
                    "error": error,
                }
            )
            return None, worker
        layers.append(
            {
                "name": name,
                "status": "pass",
                "latency_ms": round((time.monotonic() - started) * 1000.0, 1),
            }
        )
        return result, None

    async def cleanup_late_start(worker: asyncio.Task) -> None:
        try:
            result = await worker
            await asyncio.to_thread(backend.stop, result.session_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Sandbox late-session cleanup failed")

    try:
        probe = getattr(backend, "verify_credentials", None)
        if not callable(probe):
            layers.append(
                {
                    "name": "credentials",
                    "status": "fail",
                    "latency_ms": 0.0,
                    "error": "credential verification unavailable",
                }
            )
            return {
                "ready": False,
                "layers": layers
                + [
                    {"name": "connectivity", "status": "skipped", "latency_ms": 0.0},
                    {"name": "speech", "status": "skipped", "latency_ms": 0.0},
                ],
            }
        credentials, _ = await run_layer("credentials", probe, "credential verification failed")
        if credentials is None:
            return {
                "ready": False,
                "layers": layers
                + [
                    {"name": "connectivity", "status": "skipped", "latency_ms": 0.0},
                    {"name": "speech", "status": "skipped", "latency_ms": 0.0},
                ],
            }
        result, late_worker = await run_layer(
            "connectivity",
            lambda: backend.start(StartOptions(avatar_id=payload.avatar_id, is_sandbox=True)),
            "LiveAvatar or LiveKit connectivity failed",
        )
        if result is None:
            if late_worker is not None:
                asyncio.create_task(cleanup_late_start(late_worker))
            return {
                "ready": False,
                "layers": layers + [{"name": "speech", "status": "skipped", "latency_ms": 0.0}],
            }
        session_id = result.session_id
        spoken, _ = await run_layer(
            "speech",
            lambda: backend.say(session_id, payload.speech_text, generate=True),
            "speech verification failed",
        )
        return {"ready": spoken is not None, "layers": layers}
    finally:
        if session_id is not None:
            try:
                await asyncio.to_thread(backend.stop, session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Sandbox verification cleanup failed")


@router.get("/admin/config")
async def admin_config(_: None = Depends(admin_auth)) -> dict[str, Any]:
    """Sanitized config dump: present/missing for secrets, no secret values."""
    cfg = deps().config or AppConfig.from_env()

    def _present(val: str) -> str:
        return "present" if (val or "").strip() else "missing"

    return {
        "app_env": cfg.app_env,
        "render_backend": cfg.render_backend,
        "store_backend": cfg.store_backend,
        "director_enabled": cfg.director_enabled,
        "debug_enabled": cfg.debug_enabled,
        "pipecat_enabled": getattr(cfg, "pipecat_enabled", False),
        "lmcache_enabled": cfg.lmcache_enabled,
        "coverage_match_threshold": getattr(cfg, "coverage_match_threshold", 0.75),
        "llm": {
            "engine": cfg.llm.engine,
            "model": cfg.llm.model or None,
            "base_url": "present" if cfg.llm.base_url else "missing",
            "guided_json": getattr(cfg.llm, "guided_json", False),
            "stream": cfg.llm.stream,
        },
        "tts": {
            "engine": cfg.tts.engine,
            "model": cfg.tts.model or None,
            "base_url": "present" if cfg.tts.base_url else "missing",
            "preset_id": cfg.tts.preset_id,
        },
        "secrets": {
            "backend_api_token": _present(cfg.backend_api_token),
            "admin_api_token": _present(cfg.admin_api_token),
            "liveavatar_api_key": "present" if cfg.api_key_present else "missing",
            "livekit_api_key": _present(cfg.livekit_api_key),
            "livekit_api_secret": _present(cfg.livekit_api_secret),
            "avatar_base_url": _present(cfg.avatar_base_url),
            "livekit_url": _present(cfg.livekit_url),
        },
    }


@router.get("/admin/health")
async def admin_health(_: None = Depends(admin_auth)) -> dict[str, Any]:
    """Deep health — same payload as /health/ready."""
    return await health_ready()
