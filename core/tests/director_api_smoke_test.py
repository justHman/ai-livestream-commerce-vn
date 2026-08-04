"""Live smoke test: Director → say-loop via /api/v1 (cloud, sandbox).

Forces DIRECTOR_ENABLED=1 + hashing embedder (offline, deterministic) and runs
the closed loop against the REAL LiveAvatar sandbox (free, no credits):
  1. start session
  2. /lite/attach  — build Director + embed a small VN catalog
  3. /lite/ingest  — feed a price-question cluster -> Director answers via O(1)
     structured field (answer_fact, no LLM) -> avatar speaks
  4. /lite/ingest  — feed an open-ended cluster -> answer_cluster (LLM prompt)
  5. stop

Run:
    cd implementations
    DIRECTOR_ENABLED=1 DIRECTOR_EMBEDDER=hash python -m core.tests.director_api_smoke_test
"""

from __future__ import annotations

import os

os.environ.setdefault("DIRECTOR_ENABLED", "1")
os.environ.setdefault("DIRECTOR_EMBEDDER", "hash")

import pytest

pytestmark = pytest.mark.skip(
    reason="integration smoke script — needs LIVEAVATAR_API_KEY / real sandbox; "
    "run via `DIRECTOR_ENABLED=1 DIRECTOR_EMBEDDER=hash python -m core.tests.director_api_smoke_test`"
)

from fastapi.testclient import TestClient

# Import AFTER env is set so the server wires the Director runtime.
from backend.main import app


def main() -> None:
    print("== Director -> say-loop smoke (cloud sandbox, hash embedder) ==\n")
    c = TestClient(app)

    h = c.get("/api/v1/health").json()
    print(f"[health]    director_enabled={h['director_enabled']} backend={h['render_backend']}")
    assert h["director_enabled"], "DIRECTOR_ENABLED must wire the runtime"

    sid = c.post("/api/v1/sessions", json={"is_sandbox": True}).json()["session_id"]
    print("[start]     session ok")

    products = [
        {
            "id": "P002",
            "name": "Serum Vitamin C",
            "description": "Serum làm sáng da mờ thâm",
            "price": 329000,
            "original_price": 490000,
            "colors": [],
            "sizes": [],
            "shipping": "Freeship đơn từ 200k, giao 2-4 ngày",
        },
        {
            "id": "P003",
            "name": "Áo thun đen",
            "description": "Áo cotton form rộng unisex",
            "price": 149000,
            "colors": ["đen", "trắng"],
            "sizes": ["M", "L", "XL"],
            "material": "cotton 100%",
        },
    ]
    a = c.post(f"/api/v1/sessions/{sid}/attach", json={"products": products}).json()
    print(f"[attach]    {a['products']} embedder={a['embedder']}")

    # Price question -> should resolve to O(1) structured field (answer_fact)
    r1 = c.post(
        f"/api/v1/sessions/{sid}/ingest",
        json={
            "viewer_count": 10,
            "msg_rate": 3.0,
            "comments": [
                {"text": "Serum giá bao nhiêu shop?"},
                {"text": "serum bao nhiêu tiền vậy"},
            ],
        },
    ).json()
    print(
        f"[ingest#1]  action={r1['action']} field={r1.get('field')} product={r1.get('product_id')}"
    )
    print(f"            spoken={str(r1.get('spoken'))[:60]!r}")

    # Open-ended -> LLM prompt path (answer_cluster)
    r2 = c.post(
        f"/api/v1/sessions/{sid}/ingest",
        json={
            "viewer_count": 10,
            "msg_rate": 3.0,
            "comments": [{"text": "Áo thun mặc có nóng không shop, chất có thoáng không"}],
        },
    ).json()
    print(
        f"[ingest#2]  action={r2['action']} product={r2.get('product_id')} interrupt={r2.get('may_interrupt')}"
    )

    c.post(f"/api/v1/sessions/{sid}/stop")
    print("[stop]      [OK]")

    assert r1["action"] in ("answer_fact", "answer_cluster")
    print("\n== Director say-loop OK ==")


if __name__ == "__main__":
    main()
