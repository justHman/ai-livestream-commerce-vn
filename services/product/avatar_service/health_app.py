"""Minimal avatar health + idle frame stub for ALB / LiveKit bridge checks."""

from __future__ import annotations

from io import BytesIO
from fastapi import FastAPI, Response
from fastapi.responses import StreamingResponse

app = FastAPI(title="ai-live-avatar", version="0.0.1")

_IDLE_FRAME_BYTES: bytes | None = None


def _generate_idle_frame() -> bytes:
    """Return a tiny 320x240 teal solid JPEG — placeholder for real avatar frames."""
    from PIL import Image

    img = Image.new("RGB", (320, 240), (32, 128, 160))  # teal idle colour
    buf = BytesIO()
    img.save(buf, "JPEG", quality=75)
    return buf.getvalue()


@app.on_event("startup")
async def _warm_idle_frame() -> None:
    global _IDLE_FRAME_BYTES
    _IDLE_FRAME_BYTES = _generate_idle_frame()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "avatar"}


@app.get("/idle/frame.jpg")
def idle_frame() -> Response:
    if _IDLE_FRAME_BYTES is None:
        return Response(status_code=503, content="frame not ready")
    return StreamingResponse(
        BytesIO(_IDLE_FRAME_BYTES), media_type="image/jpeg"
    )
