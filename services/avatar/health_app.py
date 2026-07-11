"""Minimal avatar health surface for skeleton image / ALB checks."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="ai-live-avatar", version="0.0.1")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "avatar"}
