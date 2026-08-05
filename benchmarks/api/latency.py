"""Benchmark api.livento.me throughput + latency. Saves JSON log.

Usage:
  python scripts/bench_api.py --base https://api.livento.me --token $BK --out .runtime/bench-<ts>.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime


def _req(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, float]:
    headers = {"Authorization": f"Bearer {token}"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            r.read()
        return r.status, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, time.perf_counter() - t0
    except Exception:
        return 0, time.perf_counter() - t0


def bench(url: str, token: str, method: str, body: dict | None, n: int, c: int) -> dict:
    lat: list[float] = []
    ok = 0
    bad = 0
    t0 = time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=c) as ex:
        futs = [ex.submit(_req, method, url, token, body) for _ in range(n)]
        for f in cf.as_completed(futs):
            code, dt = f.result()
            lat.append(dt)
            if code == 200:
                ok += 1
            else:
                bad += 1
    wall = time.perf_counter() - t0
    lat.sort()
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    p99 = lat[int(len(lat) * 0.99)] if lat else 0
    return {
        "endpoint": url,
        "method": method,
        "concurrency": c,
        "total": n,
        "ok": ok,
        "bad": bad,
        "rps": round(n / wall, 1) if wall else 0,
        "p50_ms": round(p50 * 1000, 1),
        "p95_ms": round(p95 * 1000, 1),
        "p99_ms": round(p99 * 1000, 1),
        "wall_s": round(wall, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="https://api.livento.me")
    ap.add_argument("--token", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    results = []
    base = args.base.rstrip("/")
    # health/live — c1, c5, c20
    for c in (1, 5, 20):
        r = bench(f"{base}/api/v1/health/live", args.token, "GET", None, 100, c)
        results.append(r)
        print(json.dumps(r))
    # lite/say — c5 (30 req) needs session
    sid_out = urllib.request.urlopen(
        urllib.request.Request(
            f"{base}/api/v1/lite/start",
            data=b"{}",
            headers={"Authorization": f"Bearer {args.token}", "Content-Type": "application/json"},
            method="POST",
        ),
        timeout=30,
    )
    sid = json.loads(sid_out.read())["session_id"]
    body = {"session_id": sid, "text": "Gioi thieu san pham giup minh"}
    r = bench(f"{base}/api/v1/lite/say", args.token, "POST", body, 30, 5)
    r["session_id"] = sid
    results.append(r)
    print(json.dumps(r))
    # stop
    _req("POST", f"{base}/api/v1/lite/stop", args.token, {"session_id": sid})

    log = {"timestamp": datetime.utcnow().isoformat() + "Z", "base": base, "results": results}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(log, f, indent=2)
    print(f"\nSaved {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())