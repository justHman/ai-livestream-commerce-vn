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
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ...config import AppConfig
from ...render.base import RenderBackend
from ...render.mock import MockRenderBackend
from ...render.queue import BoundedVideoQueue
from ...render.locks import SessionLockRegistry
from ..auth import viewer_auth

# Phase B coordinator — optional, constructed in server.py.
# Imported here for type annotations; actual import is safe (no heavy deps).
from ...director.coordinator import DirectorCoordinator

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
    from ...schemas.run_plan import (
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
    from ...config import AppConfig

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
        from ...director.embedder import embedder_status

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
    from ...livekit_tokens import LiveKitConfigError, mint_session_viewer_token

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



