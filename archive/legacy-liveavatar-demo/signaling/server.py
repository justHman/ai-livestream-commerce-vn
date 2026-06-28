"""FastAPI Signaling Server — SDP offer/answer for WebRTC.

Minimal signaling: one POST endpoint receives an SDP offer from the
browser and returns an SDP answer. The server creates a PeerConnection
with the AvatarVideoTrack and negotiates media.

Architecture:
    Browser                    FastAPI                    Pipeline
    ┌───────┐                  ┌────────────┐             ┌───────┐
    │<video>│──SDP offer──────►│ POST /offer│             │orchestr.│
    │       │                  │            │             │       │
    │       │◄─SDP answer──────│PeerConnection│◄─frames───│frame  │
    │       │────WebRTC───────►│            │             │queue  │
    └───────┘                  └────────────┘             └───────┘
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

try:
    from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack
    from aiortc.sdp import session_description_to_sdp
except ImportError:
    RTCPeerConnection = None  # type: ignore[assignment,misc]
    RTCSessionDescription = None  # type: ignore[assignment,misc]

from .webrtc_track import AvatarVideoTrack, FrameQueue

logger = logging.getLogger(__name__)


@dataclass
class SignalingServerConfig:
    """Signaling server configuration."""

    host: str = "0.0.0.0"
    port: int = 8000
    static_dir: Optional[str] = None


class SignalingServer:
    """WebRTC signaling server using FastAPI.

    Parameters
    ----------
    frame_queue : FrameQueue
        Queue that the pipeline pushes frames into.
    config : SignalingServerConfig
        Server configuration.
    """

    def __init__(
        self,
        frame_queue: FrameQueue,
        config: Optional[SignalingServerConfig] = None,
    ) -> None:
        self.config = config or SignalingServerConfig()
        self.frame_queue = frame_queue
        self.app = FastAPI(title="LiveAvatar Signaling")
        self._pcs: set = set()
        self._setup_routes()

    def _setup_routes(self) -> None:
        """Register FastAPI routes."""

        @self.app.post("/offer")
        async def handle_offer(request: Request):
            """Handle SDP offer from browser, return SDP answer."""
            if RTCPeerConnection is None:
                return JSONResponse(
                    {"error": "aiortc not installed"},
                    status_code=500,
                )

            body = await request.json()
            offer_sdp = body.get("sdp", "")
            offer_type = body.get("type", "offer")

            # Create peer connection
            pc = RTCPeerConnection()
            self._pcs.add(pc)

            @pc.on("connectionstatechange")
            async def on_state():
                logger.info(f"PeerConnection state: {pc.connectionState}")
                if pc.connectionState in ("failed", "closed"):
                    self._pcs.discard(pc)

            # Add video track from frame queue
            async def frame_gen():
                async for frame, meta in self.frame_queue.async_generator():
                    yield frame, meta

            track = AvatarVideoTrack(
                frame_generator=frame_gen(),
                fps=24,
            )
            pc.addTrack(track)

            # Set remote description (offer)
            offer = RTCSessionDescription(sdp=offer_sdp, type=offer_type)
            await pc.setRemoteDescription(offer)

            # Create answer
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            return JSONResponse(
                {
                    "sdp": pc.localDescription.sdp,
                    "type": pc.localDescription.type,
                }
            )

        @self.app.get("/status")
        async def status():
            """Return signaling server status."""
            return {
                "active_connections": len(self._pcs),
                "frame_queue_size": self.frame_queue._queue.qsize(),
            }

        # Serve static files for the WebRTC viewer HTML
        if self.config.static_dir:
            self.app.mount(
                "/static",
                StaticFiles(directory=self.config.static_dir),
                name="static",
            )

    async def cleanup(self) -> None:
        """Close all peer connections."""
        for pc in self._pcs:
            await pc.close()
        self._pcs.clear()

    def run(self) -> None:
        """Run the signaling server (blocking)."""
        import uvicorn

        uvicorn.run(
            self.app,
            host=self.config.host,
            port=self.config.port,
            log_level="info",
        )
