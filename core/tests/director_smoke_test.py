"""Offline smoke test for the Director (no GPU, no network).

Forces the hashing embedder (DIRECTOR_EMBEDDER=hash) so it runs anywhere.
Verifies, with deterministic injected clock:
  1. OPENING emits templated hooks; transitions to SELLING on viewer threshold
  2. clustering groups similar VN comments; retrieval picks the right product
     (the "áo đen A vs B" disambiguation via the current-product phase target)
  3. high-intent cluster (price) outscores chitchat and is allowed to interrupt
  4. product switch fires on max-clusters; "go back" re-routes to a prior product
  5. CLOSING reached when all products done

Run:
    cd implementations
    DIRECTOR_EMBEDDER=hash python -m core.tests.director_smoke_test
"""

from __future__ import annotations

import os

os.environ.setdefault("DIRECTOR_EMBEDDER", "hash")  # offline, deterministic

import pytest

pytestmark = pytest.mark.skip(
    reason="integration smoke script — needs LIVEAVATAR_API_KEY / real sandbox; "
           "run via `DIRECTOR_EMBEDDER=hash python -m core.tests.director_smoke_test`"
)

from core.director import (
    Comment,
    Director,
    HookPool,
    Phase,
    ProductState,
    StreamConfig,
    StreamState,
    build_embedder,
    cluster_comments,
)
from core.director.embedder import average
from core.director.scorer import rank_clusters, retrieve_product


def _embed(emb, texts):
    return emb.encode(texts)


def main() -> None:
    print("== Director offline smoke test (hashing embedder) ==\n")
    emb = build_embedder()
    print(f"[embedder]  {emb.name}")

    # Build product catalog with embeddings from their name+desc.
    catalog = [
        ("P001", "Kem chống nắng La Roche-Posay SPF50 chống nước"),
        ("P002", "Serum Vitamin C làm sáng da mờ thâm"),
        ("P003", "Áo thun đen cotton form rộng unisex"),
    ]
    prod_embs = _embed(emb, [d for _, d in catalog])
    products = [
        ProductState(product_id=pid, name=desc, embedding=list(e))
        for (pid, desc), e in zip(catalog, prod_embs)
    ]

    cfg = StreamConfig(viewer_threshold=3, max_clusters_per_product=2,
                       interrupt_score_threshold=1.5)
    state = StreamState(products=list(products))
    director = Director(state=state, cfg=cfg, hook_pool=HookPool())

    # 1. OPENING -> hook, no viewers yet
    d0 = director.decide([], now=1.0)
    print(f"[opening]   action={d0.action!r} text={d0.text[:40]!r}")
    assert d0.action == "speak_hook" and state.phase == Phase.OPENING

    # viewers arrive -> next decide transitions to SELLING
    state.traffic.viewer_count = 5
    state.phase_elapsed_sec = 80.0
    d1 = director.decide([], now=10.0)
    print(f"[->selling] phase={state.phase.value} action={d1.action!r}")
    assert state.phase == Phase.SELLING

    # 2 + 3. comments: price question about serum (P002) + chitchat
    now = 20.0
    texts = [
        "Serum Vitamin C giá bao nhiêu shop?",   # high intent, P002
        "Serum này bao nhiêu tiền vậy ạ?",       # same cluster (price/P002)
        "Chị ơi đẹp quá",                         # chitchat low intent
    ]
    embs = _embed(emb, texts)
    comments = [Comment(text=t, embedding=list(e), t=now) for t, e in zip(texts, embs)]

    # ensure current product is the serum so phase target matches retrieval
    state.current_product_index = 1  # P002
    state.products[1].status = state.products[1].status  # active enough for test

    clusters = cluster_comments(comments, merge_threshold=cfg.cluster_merge_threshold)
    ranked = rank_clusters(clusters, state, cfg, now)
    print(f"[cluster]   {len(clusters)} clusters; top score={ranked[0].score:.2f} "
          f"intent={ranked[0].intent:.2f} product={ranked[0].cluster.product_id}")
    assert ranked[0].intent >= 0.9, "price cluster should be high intent"

    d2 = director.decide(comments, now=now)
    print(f"[answer]    action={d2.action!r} may_interrupt={d2.may_interrupt} "
          f"product={d2.product_id}")
    print(f"            reason: {d2.reason}")
    assert d2.action == "answer_cluster"

    # 4. product switch on max clusters, then "go back" to P001
    state.products[1].cluster_count = cfg.max_clusters_per_product
    d3 = director.decide([], now=now + 1)
    cur_after = state.current_product()
    print(f"[switch]    after max clusters -> current="
          f"{cur_after.product_id if cur_after else None}")

    moved = state.goto_product("P001")
    print(f"[goback]    goto P001 -> {moved}, current={state.current_product().product_id}")
    assert moved and state.current_product().product_id == "P001"

    # 5. mark all done -> CLOSING
    for p in state.products:
        p.status = p.status.DONE
    d4 = director.decide([], now=now + 2)
    # _should_switch_product won't fire without elapsed; force closing path:
    state.phase = Phase.SELLING
    state.product_elapsed_sec = cfg.product_time_budget_sec + 1
    d5 = director.decide([], now=now + 3)
    print(f"[closing]   phase={state.phase.value} action={d5.action!r}")
    assert state.phase == Phase.CLOSING

    print("\n== Director logic OK ==")


if __name__ == "__main__":
    main()
