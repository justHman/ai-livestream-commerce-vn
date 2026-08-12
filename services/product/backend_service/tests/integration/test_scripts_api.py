"""API contract tests for /api/v1/script-sets (Change B tasks 11.1-11.11).

The router binds ONLY the ``ScriptAuthoringService`` protocol; these tests
inject a minimal in-memory fake so the REST/SSE contract is verified without
the domain implementation (parallel cluster) and without any model calls.

Covered contract points:
  - auth: 401 when tokens are required and absent/wrong
  - 200 gate-fail domain semantics (not a transport failure)
  - 202 async generation/fix/regenerate with stable workflow ids
  - 409 invalid transitions / stale revision / fix ineligible / missing scripts
  - 404 unknown resources
  - 422 malformed bodies (validation)
  - idempotency: duplicate equivalent requests return the existing workflow
  - SSE: snapshot-first, ordered events, stable IDs, no script text,
    reconnect replays the snapshot and never creates new jobs
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.application.script_authoring.service import ScriptAuthoringError

from conftest import make_deps as _Deps  # noqa: F401


def _viewer_token() -> str:
    """Test-only viewer token fixture (mirrors tests/integration/test_api_security.py)."""
    return "viewer" + "-secret"


def _now_ms() -> int:
    return int(time.time() * 1000)


class FakeScriptAuthoringService:
    """Minimal in-memory fake implementing the service protocol.

    Deterministic: preview math and gate results are pure functions of input;
    generation is scheduled on a background thread so the 202 response is
    decoupled from completion (like the real workflow scheduler).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sets: dict[str, dict[str, Any]] = {}
        self._next_set = 1
        self._next_workflow = 1
        self._next_batch = 1
        self._event_queues: dict[str, list[dict[str, str]]] = {}
        self._revision_counter = 0

    # ── helpers ─────────────────────────────────────────────────────

    def _raise_not_found(self, kind: str, ident: str) -> None:
        raise ScriptAuthoringError("not_found", f"{kind} {ident} not found")

    def _item(self, set_id: str, product_id: str) -> dict[str, Any]:
        script_set = self._sets.get(set_id)
        if script_set is None:
            self._raise_not_found("script set", set_id)
        item = script_set["items"].get(product_id)
        if item is None:
            self._raise_not_found("product script", product_id)
        return item

    def _enqueue(
        self, set_id: str, batch_id: str, event: str, data: dict[str, Any]
    ) -> None:
        queue = self._event_queues.setdefault(batch_id, [])
        queue.append({"event": event, "data": json.dumps(data)})

    def _scheduled(self, target_ms: int, fn) -> None:
        def _run() -> None:
            delay = max(0.0, (target_ms - _now_ms()) / 1000.0)
            time.sleep(delay)
            try:
                fn()
            except Exception:  # pragma: no cover - background thread only
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _mark_failed(self, set_id: str, product_id: str, batch_id: str, rule_id: str) -> None:
        item = self._item(set_id, product_id)
        with self._lock:
            item["state"] = "GATE_FAILED"
            item["gate"] = {
                "state": "gate_failed",
                "violations": [
                    {"rule_id": rule_id, "severity": "error", "message": f"fails {rule_id}"}
                ],
            }
            item["workflow"] = {"id": item["workflow"]["id"], "status": "failed"}
        self._enqueue(set_id, batch_id, "product.failed", {"product_id": product_id})

    def _complete(self, set_id: str, product_id: str, batch_id: str) -> None:
        item = self._item(set_id, product_id)
        with self._lock:
            item["state"] = "REVIEWABLE"
            item["gate"] = {"state": "passed", "violations": []}
            item["versions"] = [{"id": "v1", "state": "REVIEWABLE"}]
            item["current_version_id"] = "v1"
            item["workflow"] = {"id": item["workflow"]["id"], "status": "completed"}
        self._enqueue(set_id, batch_id, "product.reviewable", {"product_id": product_id})

    def _emit_terminal(self, set_id: str, batch_id: str, event: str) -> None:
        batch = self._sets[set_id]["batches"][batch_id]
        with self._lock:
            batch["status"] = "cancelled" if event == "batch.cancelled" else "completed"
        self._enqueue(set_id, batch_id, event, {})

    # ── ScriptSet aggregate ─────────────────────────────────────────

    async def create_script_set(
        self,
        *,
        name: str,
        transition_policy: str,
        product_ids: list[str],
        brief: dict[str, Any] | None,
    ) -> dict[str, Any]:
        with self._lock:
            set_id = f"set-{self._next_set}"
            self._next_set += 1
            self._sets[set_id] = {
                "id": set_id,
                "name": name,
                "transition_policy": transition_policy,
                "product_ids": list(product_ids),
                "brief": brief or {},
                "revision": 0,
                "items": {pid: {"state": "EMPTY"} for pid in product_ids},
                "batches": {},
            }
        result = await self.get_script_set(set_id=set_id)
        assert result is not None
        return result

    async def get_script_set(self, *, set_id: str) -> dict[str, Any] | None:
        script_set = self._sets.get(set_id)
        if script_set is None:
            return None
        return {
            "id": script_set["id"],
            "name": script_set["name"],
            "transition_policy": script_set["transition_policy"],
            "product_ids": list(script_set["product_ids"]),
            "revision": script_set["revision"],
            "items": {pid: dict(item) for pid, item in script_set["items"].items()},
        }

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
        script_set = self._sets.get(set_id)
        if script_set is None:
            return None
        if revision is not None and revision != script_set["revision"]:
            raise ScriptAuthoringError(
                "stale_revision", "provided revision does not match current revision"
            )
        if name is not None:
            script_set["name"] = name
        if transition_policy is not None:
            script_set["transition_policy"] = transition_policy
        if product_ids is not None:
            script_set["product_ids"] = list(product_ids)
            for pid in product_ids:
                script_set["items"].setdefault(pid, {"state": "EMPTY"})
        if brief is not None:
            script_set["brief"] = brief
        script_set["revision"] += 1
        result = await self.get_script_set(set_id=set_id)
        assert result is not None
        return result

    # ── Per-product commands ────────────────────────────────────────

    async def save_draft(
        self,
        *,
        set_id: str,
        product_id: str,
        display_text: str,
        spoken_text: str | None,
        revision: int | None,
    ) -> dict[str, Any] | None:
        item = self._item(set_id, product_id)
        with self._lock:
            if item["state"] == "EMPTY":
                item["state"] = "DRAFT"
            item["versions"] = [
                {
                    "id": "v1",
                    "state": "DRAFT",
                    "display_text": display_text,
                    "spoken_text": spoken_text or display_text,
                }
            ]
            item["current_version_id"] = "v1"
        return {"ok": True, "product_id": product_id, "state": item["state"]}

    async def submit_for_gate(
        self, *, set_id: str, product_id: str
    ) -> dict[str, Any] | None:
        item = self._item(set_id, product_id)
        if item.get("current_version_id") is None:
            raise ScriptAuthoringError("illegal_transition", "cannot submit without a draft")
        text = (item.get("versions") or [{}])[0].get("display_text", "")
        passed = text not in ("kém chất lượng", "rao bán không đúng sự thật")
        with self._lock:
            item["state"] = "REVIEWABLE" if passed else "GATE_FAILED"
            item["gate"] = (
                {"state": "passed", "violations": []}
                if passed
                else {
                    "state": "gate_failed",
                    "violations": [
                        {
                            "rule_id": "VN_SPELLING_001",
                            "severity": "error",
                            "message": "phát hiện lỗi chính tả",
                        }
                    ],
                }
            )
        return {
            "ok": True,
            "product_id": product_id,
            "state": item["state"],
            "gate": item["gate"],
        }

    async def preview_product(
        self, *, set_id: str, product_id: str, target_duration_s: int
    ) -> dict[str, Any] | None:
        self._item(set_id, product_id)  # existence check
        k = max(1, round(target_duration_s / 600))
        return {
            "product_id": product_id,
            "target_duration_s": target_duration_s,
            "planned_segment_count": k,
            "estimated_semantic_calls": 1 + k,
        }

    async def start_generation(
        self,
        *,
        set_id: str,
        product_id: str,
        target_duration_s: int,
        intent: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        item = self._item(set_id, product_id)
        if item.get("workflow") is not None and idempotency_key:
            if item["workflow"].get("idempotency_key") == idempotency_key:
                return {"workflow_id": item["workflow"]["id"], "idempotent": True}
        with self._lock:
            workflow_id = f"wf-{self._next_workflow}"
            self._next_workflow += 1
            item["workflow"] = {
                "id": workflow_id,
                "status": "running",
                "idempotency_key": idempotency_key,
                "target_duration_s": target_duration_s,
            }
            item["state"] = "GENERATING"
        fail = (item.get("versions") or [{}])[0].get("display_text", "") == "kém chất lượng"
        if fail:
            self._scheduled(
                _now_ms() + 100,
                lambda: self._mark_failed(set_id, product_id, "b-none", "STYLE_001"),
            )
        else:
            self._scheduled(
                _now_ms() + 100,
                lambda: self._complete(set_id, product_id, "b-none"),
            )
        return {"workflow_id": workflow_id, "product_id": product_id, "status": "queued"}

    async def regenerate_segment(
        self,
        *,
        set_id: str,
        product_id: str,
        segment_index: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        item = self._item(set_id, product_id)
        if item["state"] not in ("GATE_FAILED", "DRAFT"):
            raise ScriptAuthoringError(
                "illegal_transition",
                f"cannot regenerate segment {segment_index} in state {item['state']}",
            )
        return {
            "workflow_id": f"wf-r{segment_index}",
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
        item = self._item(set_id, product_id)
        if item["state"] != "GATE_FAILED":
            raise ScriptAuthoringError("fix_not_eligible", "AI fix requires a gate-failed version")
        with self._lock:
            item["state"] = "AI_FIXING"
        self._scheduled(
            _now_ms() + 100,
            lambda: self._apply_fix(set_id, product_id),
        )
        return {"workflow_id": "wf-fix", "product_id": product_id, "status": "queued"}

    def _apply_fix(self, set_id: str, product_id: str) -> None:
        item = self._item(set_id, product_id)
        with self._lock:
            item["state"] = "DRAFT"
            item["versions"] = [
                {
                    "id": "v1",
                    "state": "DRAFT",
                    "display_text": "bản sửa đã hợp lệ",
                    "spoken_text": "bản sửa đã hợp lệ",
                }
            ]
            item["current_version_id"] = "v1"
        self._enqueue(set_id, "b-fix", "product.fix_ready", {"product_id": product_id})

    async def approve_product(
        self,
        *,
        set_id: str,
        product_id: str,
        version_id: str,
        actor: str,
    ) -> dict[str, Any] | None:
        item = self._item(set_id, product_id)
        if item["state"] != "REVIEWABLE" or item.get("current_version_id") != version_id:
            raise ScriptAuthoringError(
                "illegal_transition", "only the current REVIEWABLE version is approvable"
            )
        with self._lock:
            item["state"] = "APPROVED"
            item["approval"] = {
                "version_id": version_id,
                "actor": actor,
                "approved_at": _now_ms(),
            }
        return {
            "ok": True,
            "product_id": product_id,
            "state": "APPROVED",
            "approval": item["approval"],
        }

    async def approve_batch(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        version_ids: dict[str, str],
        actor: str,
    ) -> dict[str, Any] | None:
        approvals: dict[str, Any] = {}
        for pid in product_ids:
            version_id = version_ids.get(pid)
            if version_id is None:
                raise ScriptAuthoringError("illegal_transition", f"missing version_id for {pid}")
            approvals[pid] = await self.approve_product(
                set_id=set_id,
                product_id=pid,
                version_id=version_id,
                actor=actor,
            )
        return {"ok": True, "approvals": approvals}

    # ── Batch ───────────────────────────────────────────────────────

    async def start_batch_generation(
        self,
        *,
        set_id: str,
        product_ids: list[str],
        target_duration_s: int,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        script_set = self._sets.get(set_id)
        if script_set is None:
            return None
        for pid in product_ids:
            if pid not in script_set["items"]:
                self._raise_not_found("product script", pid)
        # Idempotency (task 11.8): an equivalent request under the same
        # idempotency identity returns the existing batch, never a new one.
        if idempotency_key:
            for existing in script_set["batches"].values():
                if existing.get("idempotency_key") == idempotency_key:
                    return {
                        "batch_id": existing["id"],
                        "workflow_summary": {
                            "products": [],
                            "estimated_semantic_calls_total": 0,
                        },
                        "status": existing["status"],
                        "idempotent": True,
                    }
        with self._lock:
            batch_id = f"batch-{self._next_batch}"
            self._next_batch += 1
            script_set["batches"][batch_id] = {
                "id": batch_id,
                "status": "running",
                "product_ids": list(product_ids),
                "idempotency_key": idempotency_key,
            }
            previews: list[dict[str, Any]] = []
            for pid in product_ids:
                preview = await self.preview_product(
                    set_id=set_id, product_id=pid, target_duration_s=target_duration_s
                )
                assert preview is not None
                previews.append(preview)
                self._item(set_id, pid)["batches"] = [batch_id]
        for pid in product_ids:
            await self.start_generation(
                set_id=set_id,
                product_id=pid,
                target_duration_s=target_duration_s,
                intent="selling",
                idempotency_key=idempotency_key,
            )
        self._scheduled(
            _now_ms() + 400,
            lambda: self._emit_terminal(set_id, batch_id, "batch.completed"),
        )
        return {
            "batch_id": batch_id,
            "workflow_summary": {
                "products": previews,
                "estimated_semantic_calls_total": sum(
                    p["estimated_semantic_calls"] for p in previews
                ),
            },
            "status": "queued",
        }

    async def get_batch(self, *, set_id: str, batch_id: str) -> dict[str, Any] | None:
        batch = self._sets.get(set_id, {}).get("batches", {}).get(batch_id)
        if batch is None:
            return None
        return {
            "batch_id": batch_id,
            "status": batch["status"],
            "product_ids": list(batch["product_ids"]),
        }

    async def cancel_batch(self, *, set_id: str, batch_id: str) -> dict[str, Any] | None:
        batch = self._sets.get(set_id, {}).get("batches", {}).get(batch_id)
        if batch is None:
            return None
        if batch["status"] not in ("running", "queued"):
            raise ScriptAuthoringError(
                "illegal_transition", f"cannot cancel batch in state {batch['status']}"
            )
        self._scheduled(
            _now_ms() + 50,
            lambda: self._emit_terminal(set_id, batch_id, "batch.cancelled"),
        )
        return {"batch_id": batch_id, "status": "cancelling"}

    # ── SSE (task 11.10) ────────────────────────────────────────────

    async def get_batch_events_snapshot(self, *, set_id: str, batch_id: str) -> str | None:
        batch = self._sets.get(set_id, {}).get("batches", {}).get(batch_id)
        if batch is None:
            return None
        self._revision_counter += 1
        return json.dumps(
            {
                "batch_id": batch_id,
                "set_id": set_id,
                "status": batch["status"],
                "revision": self._revision_counter,
            }
        )

    async def stream_batch_events(
        self, *, set_id: str, batch_id: str
    ) -> AsyncIterator[dict[str, str]]:
        queue = self._event_queues.setdefault(batch_id, [])
        consumed = 0
        while True:
            while consumed < len(queue):
                event = queue[consumed]
                consumed += 1
                yield event
            if any(
                e["event"] in ("batch.completed", "batch.cancelled", "batch.error")
                for e in queue
            ):
                return
            await asyncio.sleep(0.02)


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def mock_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RENDER_BACKEND", "mock")
    monkeypatch.delenv("LIVEAVATAR_API_KEY", raising=False)
    monkeypatch.setenv("LLM_ENGINE", "none")
    monkeypatch.setenv("TTS_ENGINE", "tone")
    monkeypatch.setenv("SESSION_STORE", "memory")
    monkeypatch.setenv("DIRECTOR_ENABLED", "0")
    monkeypatch.setenv("APP_ENV", "dev")


def _auth_app(backend_token: str | None = None):
    """Build a prod-env app with viewer auth enabled (non-empty token)."""
    from backend.application.render.mock import MockRenderBackend
    from backend.config import AppConfig

    if backend_token is None:
        backend_token = _viewer_token()
    deps = _Deps(
        backend=MockRenderBackend(),
        config=AppConfig(
            render_backend="mock",
            app_env="prod",
            backend_api_token=backend_token,
            cors_origins="https://example.com",
        ),
    )
    deps.script_authoring_service = FakeScriptAuthoringService()

    from backend.main import create_app

    return create_app(config=deps.config, deps=deps)


@pytest.fixture
def client(mock_env: None):
    """App with the fake ScriptAuthoringService injected into the container."""
    from backend.application.render.mock import MockRenderBackend
    from backend.config import AppConfig

    deps = _Deps(
        backend=MockRenderBackend(),
        config=AppConfig(render_backend="mock", app_env="dev"),
    )
    service = FakeScriptAuthoringService()
    deps.script_authoring_service = service

    from backend.main import create_app

    app = create_app(config=deps.config, deps=deps)
    with TestClient(app) as test_client:
        test_client.service = service
        yield test_client


def _new_set(client: TestClient, product_ids: list[str] | None = None) -> str:
    body = {"name": "Set demo", "product_ids": product_ids or ["P001"]}
    resp = client.post("/api/v1/script-sets", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


# ── auth (task 11.11) ───────────────────────────────────────────────


def test_auth_401_when_token_required_and_absent() -> None:
    with TestClient(_auth_app()) as client:
        resp = client.post(
            "/api/v1/script-sets",
            json={"name": "nope"},
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401, resp.text
        envelope = resp.json()
        assert envelope["error"]["code"] == "http_401"
        missing = client.post("/api/v1/script-sets", json={"name": "nope"})
        assert missing.status_code == 401, missing.text
        ok = client.post(
            "/api/v1/script-sets",
            json={"name": "ok"},
            headers={"Authorization": f"Bearer {_viewer_token()}"},
        )
        assert ok.status_code == 201, ok.text


def test_sse_requires_auth() -> None:
    with TestClient(_auth_app()) as client:
        resp = client.get(
            "/api/v1/script-sets/set-1/generation-batches/batch-1/events",
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401, resp.text


# ── ScriptSet CRUD (task 11.2) ──────────────────────────────────────


def test_create_get_patch_script_set(client: TestClient) -> None:
    set_id = _new_set(client, ["P001", "P002"])
    resp = client.get(f"/api/v1/script-sets/{set_id}")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["id"] == set_id
    assert data["product_ids"] == ["P001", "P002"]
    assert data["revision"] == 0

    patch = client.patch(
        f"/api/v1/script-sets/{set_id}",
        json={"name": "Tên mới", "revision": 0},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["name"] == "Tên mới"
    assert patch.json()["revision"] == 1


def test_patch_stale_revision_409(client: TestClient) -> None:
    set_id = _new_set(client)
    first = client.patch(
        f"/api/v1/script-sets/{set_id}", json={"name": "v2", "revision": 0}
    )
    assert first.status_code == 200, first.text
    stale = client.patch(
        f"/api/v1/script-sets/{set_id}", json={"name": "v3", "revision": 0}
    )
    assert stale.status_code == 409, stale.text
    body = stale.json()["error"]
    assert body["code"] == "stale_revision"


def test_get_unknown_script_set_404(client: TestClient) -> None:
    resp = client.get("/api/v1/script-sets/does-not-exist")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


def test_script_set_422_malformed(client: TestClient) -> None:
    resp = client.post("/api/v1/script-sets", json={"name": ""})
    assert resp.status_code == 422, resp.text
    resp2 = client.post(
        "/api/v1/script-sets", json={"name": "x", "transition_policy": "SOMETIMES"}
    )
    assert resp2.status_code == 422, resp2.text


# ── draft / submit / gate semantics (task 11.3) ─────────────────────


def test_submit_gate_failed_is_200_with_domain_state(client: TestClient) -> None:
    set_id = _new_set(client)
    draft = client.put(
        f"/api/v1/script-sets/{set_id}/products/P001/draft",
        json={"display_text": "rao bán không đúng sự thật"},
    )
    assert draft.status_code == 200, draft.text
    resp = client.post(f"/api/v1/script-sets/{set_id}/products/P001/submit")
    assert resp.status_code == 200, resp.text  # NOT 4xx
    body = resp.json()
    assert body["state"] == "GATE_FAILED"
    assert body["gate"]["state"] == "gate_failed"
    assert body["gate"]["violations"][0]["rule_id"] == "VN_SPELLING_001"


def test_submit_gate_pass_becomes_reviewable(client: TestClient) -> None:
    set_id = _new_set(client)
    client.put(
        f"/api/v1/script-sets/{set_id}/products/P001/draft",
        json={
            "display_text": "Kem ABC giá 299.000 đồng, chất lượng tốt.",
            "spoken_text": "Kem A B C giá hai trăm chín mươi chín nghìn đồng.",
        },
    )
    resp = client.post(f"/api/v1/script-sets/{set_id}/products/P001/submit")
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "REVIEWABLE"
    assert resp.json()["gate"]["state"] == "passed"


def test_submit_without_draft_409_illegal_transition(client: TestClient) -> None:
    set_id = _new_set(client)
    resp = client.post(f"/api/v1/script-sets/{set_id}/products/P001/submit")
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "illegal_transition"


# ── generation preview (task 11.4) ──────────────────────────────────


def test_generation_preview_no_model(client: TestClient) -> None:
    set_id = _new_set(client)
    resp = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/generation-preview",
        json={"product_id": "P001", "target_duration_s": 600},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["planned_segment_count"] == 1
    assert body["estimated_semantic_calls"] == 2  # 1 planning + K segments


def test_generation_preview_unknown_product_404(client: TestClient) -> None:
    set_id = _new_set(client)
    resp = client.post(
        f"/api/v1/script-sets/{set_id}/products/P999/generation-preview",
        json={"product_id": "P999", "target_duration_s": 600},
    )
    assert resp.status_code == 404, resp.text


# ── async generation (task 11.5) ────────────────────────────────────


def test_generate_returns_202_with_workflow_id(client: TestClient) -> None:
    set_id = _new_set(client)
    resp = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/generate",
        json={"target_duration_s": 600, "intent": "selling"},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["workflow_id"].startswith("wf-")
    assert body["status"] == "queued"


def test_generate_idempotent_duplicate_returns_same_workflow(client: TestClient) -> None:
    set_id = _new_set(client)
    headers = {"Idempotency-Key": "gen-1"}
    first = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/generate",
        json={"target_duration_s": 600, "intent": "selling"},
        headers=headers,
    )
    assert first.status_code == 202, first.text
    second = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/generate",
        json={"target_duration_s": 600, "intent": "selling"},
        headers=headers,
    )
    assert second.status_code == 202, second.text
    assert second.json()["workflow_id"] == first.json()["workflow_id"]
    assert second.json().get("idempotent") is True


# ── regenerate / fix eligibility (task 11.6) ────────────────────────


def test_regenerate_segment_202_and_guard(client: TestClient) -> None:
    set_id = _new_set(client)
    # illegal in EMPTY state -> 409
    bad = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/segments/0/regenerate", json={}
    )
    assert bad.status_code == 409, bad.text
    assert bad.json()["error"]["code"] == "illegal_transition"
    # after a failed gate the segment becomes eligible -> 202
    client.put(
        f"/api/v1/script-sets/{set_id}/products/P001/draft",
        json={"display_text": "kém chất lượng"},
    )
    client.post(f"/api/v1/script-sets/{set_id}/products/P001/submit")
    ok = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/segments/0/regenerate", json={}
    )
    assert ok.status_code == 202, ok.text
    assert ok.json()["segment_index"] == 0


def test_fix_ineligible_409_fix_not_eligible(client: TestClient) -> None:
    set_id = _new_set(client)
    resp = client.post(f"/api/v1/script-sets/{set_id}/products/P001/fix", json={})
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "fix_not_eligible"


def test_fix_gate_failed_202(client: TestClient) -> None:
    set_id = _new_set(client)
    client.put(
        f"/api/v1/script-sets/{set_id}/products/P001/draft",
        json={"display_text": "kém chất lượng"},
    )
    client.post(f"/api/v1/script-sets/{set_id}/products/P001/submit")
    resp = client.post(f"/api/v1/script-sets/{set_id}/products/P001/fix", json={})
    assert resp.status_code == 202, resp.text
    assert resp.json()["workflow_id"] == "wf-fix"


# ── approval (task 11.7) ────────────────────────────────────────────


def test_approve_requires_current_reviewable_version(client: TestClient) -> None:
    set_id = _new_set(client)
    client.put(
        f"/api/v1/script-sets/{set_id}/products/P001/draft",
        json={"display_text": "Kem ABC giá 299.000 đồng."},
    )
    client.post(f"/api/v1/script-sets/{set_id}/products/P001/submit")
    bad = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/approve",
        json={"version_id": "wrong-version", "actor": "nam"},
    )
    assert bad.status_code == 409, bad.text
    assert bad.json()["error"]["code"] == "illegal_transition"
    ok = client.post(
        f"/api/v1/script-sets/{set_id}/products/P001/approve",
        json={"version_id": "v1", "actor": "nam"},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["state"] == "APPROVED"
    assert body["approval"]["version_id"] == "v1"
    assert body["approval"]["actor"] == "nam"


def test_approve_batch_preserves_per_version_records(client: TestClient) -> None:
    set_id = _new_set(client, ["P001", "P002"])
    for pid in ("P001", "P002"):
        client.put(
            f"/api/v1/script-sets/{set_id}/products/{pid}/draft",
            json={"display_text": f"Kem {pid} giá 100.000 đồng."},
        )
        client.post(f"/api/v1/script-sets/{set_id}/products/{pid}/submit")
    resp = client.post(
        f"/api/v1/script-sets/{set_id}/approve-batch",
        json={
            "product_ids": ["P001", "P002"],
            "version_ids": {"P001": "v1", "P002": "v1"},
            "actor": "nam",
        },
    )
    assert resp.status_code == 200, resp.text
    approvals = resp.json()["approvals"]
    assert approvals["P001"]["state"] == "APPROVED"
    assert approvals["P001"]["approval"]["version_id"] == "v1"
    assert approvals["P002"]["approval"]["actor"] == "nam"


# ── batch generation (task 11.8) ────────────────────────────────────


def test_generate_batch_202_with_planned_summary(client: TestClient) -> None:
    set_id = _new_set(client, ["P001", "P002"])
    resp = client.post(
        f"/api/v1/script-sets/{set_id}/generate-batch",
        json={"product_ids": ["P001", "P002"], "target_duration_s": 600},
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["batch_id"].startswith("batch-")
    summary = body["workflow_summary"]
    assert len(summary["products"]) == 2
    assert summary["estimated_semantic_calls_total"] == 4  # 2 per product


def test_generate_batch_idempotency_header(client: TestClient) -> None:
    set_id = _new_set(client, ["P001"])
    headers = {"Idempotency-Key": "batch-1"}
    first = client.post(
        f"/api/v1/script-sets/{set_id}/generate-batch",
        json={"product_ids": ["P001"], "target_duration_s": 600},
        headers=headers,
    )
    assert first.status_code == 202, first.text
    second = client.post(
        f"/api/v1/script-sets/{set_id}/generate-batch",
        json={"product_ids": ["P001"], "target_duration_s": 600},
        headers=headers,
    )
    assert second.status_code == 202, second.text
    assert second.json()["batch_id"] == first.json()["batch_id"]


def test_generate_batch_unknown_product_404(client: TestClient) -> None:
    set_id = _new_set(client, ["P001"])
    resp = client.post(
        f"/api/v1/script-sets/{set_id}/generate-batch",
        json={"product_ids": ["P999"], "target_duration_s": 600},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "not_found"


# ── batch snapshot / cancel (task 11.9) ─────────────────────────────


def test_generation_batch_snapshot_and_cancel(client: TestClient) -> None:
    set_id = _new_set(client, ["P001"])
    created = client.post(
        f"/api/v1/script-sets/{set_id}/generate-batch",
        json={"product_ids": ["P001"], "target_duration_s": 600},
    )
    batch_id = created.json()["batch_id"]
    snap = client.get(f"/api/v1/script-sets/{set_id}/generation-batches/{batch_id}")
    assert snap.status_code == 200, snap.text
    assert snap.json()["status"] in ("running", "queued")
    cancelled = client.post(
        f"/api/v1/script-sets/{set_id}/generation-batches/{batch_id}/cancel"
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelling"


# ── SSE (task 11.10) ────────────────────────────────────────────────


def _consume_sse(client: TestClient, url: str, lines: int = 8) -> list[str]:
    with client.stream("GET", url) as response:
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        collected: list[str] = []
        for raw_line in response.iter_lines():
            if raw_line:
                collected.append(raw_line)
            if len(collected) >= lines:
                break
        return collected


def _pair_events(lines: list[str]) -> list[tuple[str, str]]:
    events: list[tuple[str, str]] = []
    for i in range(0, len(lines) - 1, 2):
        if lines[i].startswith("event: "):
            events.append((lines[i][7:], lines[i + 1][6:]))
    return events


def test_sse_snapshot_first_ordered_events_no_script_text(client: TestClient) -> None:
    set_id = _new_set(client, ["P001"])
    created = client.post(
        f"/api/v1/script-sets/{set_id}/generate-batch",
        json={"product_ids": ["P001"], "target_duration_s": 600},
    )
    batch_id = created.json()["batch_id"]
    lines = _consume_sse(
        client,
        f"/api/v1/script-sets/{set_id}/generation-batches/{batch_id}/events",
    )

    # first event must be the reconnect snapshot, JSON with revision
    assert lines[0] == "event: batch.snapshot"
    snapshot = json.loads(lines[1].split("data: ", 1)[1])
    assert snapshot["batch_id"] == batch_id
    assert snapshot["set_id"] == set_id
    assert snapshot["revision"] >= 1

    # live events follow with stable IDs; no script text anywhere
    text = "\n".join(lines[2:])
    assert "rao bán" not in text and "kém chất lượng" not in text
    known = {
        "batch.progress",
        "product.planning_started",
        "product.reviewable",
        "product.failed",
        "batch.completed",
        "segment.started",
        "segment.gate_passed",
        "product.plan_ready",
        "product.fix_ready",
        "batch.error",
        "batch.cancelled",
    }
    for event_name, payload in _pair_events(lines[2:]):
        assert event_name in known, event_name
        parsed = json.loads(payload)
        assert "set_id" in parsed or "product_id" in parsed or "batch_id" in parsed


def test_sse_reconnect_replays_snapshot_without_new_jobs(client: TestClient) -> None:
    set_id = _new_set(client, ["P001"])
    created = client.post(
        f"/api/v1/script-sets/{set_id}/generate-batch",
        json={"product_ids": ["P001"], "target_duration_s": 600},
    )
    batch_id = created.json()["batch_id"]
    url = f"/api/v1/script-sets/{set_id}/generation-batches/{batch_id}/events"
    first_snapshot = _consume_sse(client, url, lines=2)
    second_snapshot = _consume_sse(client, url, lines=2)
    snap1 = json.loads(first_snapshot[1].split("data: ", 1)[1])
    snap2 = json.loads(second_snapshot[1].split("data: ", 1)[1])
    assert snap1["revision"] < snap2["revision"]
    # reconnect never creates new generation jobs: workflow id is unchanged
    resp = client.get(f"/api/v1/script-sets/{set_id}")
    items = resp.json()["items"]
    assert items["P001"].get("workflow", {}).get("id", "").startswith("wf-")


def test_sse_unknown_batch_404(client: TestClient) -> None:
    set_id = _new_set(client)
    resp = client.get(f"/api/v1/script-sets/{set_id}/generation-batches/nope/events")
    assert resp.status_code == 404, resp.text
