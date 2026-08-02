"""LMCache metrics/health surface for skeleton image and ALB/SG checks."""

from __future__ import annotations

from fastapi import FastAPI, Response

app = FastAPI(title="ai-live-lmcache", version="0.0.1")


@app.get("/metrics")
def metrics() -> Response:
    body = (
        "# HELP lmcache_up 1 if process is up\n"
        "# TYPE lmcache_up gauge\n"
        "lmcache_up 1\n"
    )
    return Response(content=body, media_type="text/plain; version=0.0.4")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "lmcache"}
