"""Machine-agnostic baselines for the legacy Director (task 1.77).

Measures key Director operations on the fixed corpus and records
ops/sec + p50/p95 in a JSON file, with the producing commit + hardware note.
The canonical backend must MATCH (not beat) these baselines after extraction.

Hardware note: absolute latencies vary by machine; ratios (ops/sec, p50/p95
spread) are the portable signal.
"""

from __future__ import annotations

import json
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]


def _producer_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=str(REPO_ROOT),
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _hardware_note() -> str:
    return f"{platform.system()} {platform.machine()} {platform.processor() or 'unknown'}"


def _bench(name: str, fn: Callable[[], None], rounds: int = 200) -> dict:
    """Time ``fn`` over ``rounds`` calls; return ops/sec + p50/p95 (ms)."""
    latencies: list[float] = []
    start = time.perf_counter()
    for _ in range(rounds):
        t0 = time.perf_counter()
        fn()
        latencies.append((time.perf_counter() - t0) * 1000.0)
    elapsed = time.perf_counter() - start
    latencies.sort()
    p50 = latencies[len(latencies) // 2]
    p95 = latencies[int(len(latencies) * 0.95)]
    return {
        "name": name,
        "rounds": rounds,
        "ops_per_sec": round(rounds / elapsed, 1),
        "p50_ms": round(p50, 4),
        "p95_ms": round(p95, 4),
        "total_sec": round(elapsed, 4),
    }


def run_baselines() -> dict:
    """Run every baseline; returns the full report dict."""
    from backend.application.director.catalog import embedding_text, route_intent_to_field
    from backend.application.director.comment_buffer import ChatQueue
    from backend.application.director.clustering import cluster_comments
    from backend.application.director.embeddings import HashingEmbedder, cosine
    from backend.application.director.pivot import should_enter_pivot
    from backend.application.director.routing import route_comment

    from benchmarks.fixtures.corpus import (
        build_state,
        cfg_with_qa_window,
        corpus_comments,
        corpus_products,
    )

    embedder = HashingEmbedder()
    products = corpus_products()
    for product, vector in zip(products, embedder.encode([embedding_text(p) for p in products])):
        product.embedding = vector

    comments = corpus_comments(embedder)
    routed = [route_comment(c, products, "P004") for c in comments]
    state = build_state()
    state.cursor.opening_completed = True
    state.cursor.phase = "selling"

    def run_cluster() -> None:
        cluster_comments(routed, merge_threshold=0.55)

    def run_route() -> None:
        for c in comments:
            route_comment(c, products, "P004")

    def run_embed() -> None:
        embedder.encode([c.text for c in comments])

    def run_cosine() -> None:
        for i in range(len(routed) - 1):
            cosine(routed[i].embedding, routed[i + 1].embedding)

    def run_field() -> None:
        for c in comments:
            route_intent_to_field(c.text)

    def run_pivot() -> None:
        ids = [c.product_id or "?" for c in routed]
        should_enter_pivot("P004", ids, min_comments=5, enter_share=0.6)

    queue = ChatQueue("baseline-queue", max_size=500)
    now = 100.0

    def run_queue() -> None:
        queue.put("comment x", "user", ts=now)
        queue.snapshot(window_sec=75.0, now=now)

    director = None
    if True:  # build the decision-cycle baseline
        from backend.application.director.decision import Director

        state2 = build_state()
        state2.cursor.opening_completed = True
        state2.cursor.phase = "selling"
        state2.cursor.talking_point_idx = 1
        state2.product_elapsed_sec = 60.0
        director = Director(
            state=state2,
            cfg=cfg_with_qa_window(),
            catalog={p.id: p for p in products},
        )
        state2.add_comments(routed)

        def run_decision_cycle() -> None:
            director.decide(routed, now=now + 8.0)

        decision_cycle = _bench("decision_cycle", run_decision_cycle, rounds=100)
    else:  # pragma: no cover
        decision_cycle = {}

    results = {
        "report_version": "1.0.0",
        "commit": _producer_commit(),
        "hardware": _hardware_note(),
        "python": sys.version.split()[0],
        "embedder": embedder.name,
        "corpus": {"comments": len(comments), "products": len(products)},
        "metrics": [
            _bench("clustering", run_cluster),
            _bench("routing", run_route),
            _bench("embedding", run_embed),
            _bench("cosine", run_cosine),
            _bench("field_routing", run_field),
            _bench("pivot_gate", run_pivot),
            _bench("queue_drain", run_queue),
            decision_cycle,
        ],
    }
    return results


def main() -> None:
    results = run_baselines()
    out_dir = Path(__file__).resolve().parent
    path = out_dir / "baseline.json"
    path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
