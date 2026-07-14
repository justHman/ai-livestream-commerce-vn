# avatar — `imjusthman/ai-live-avatar`

Avatar-server skeleton for **g4dn.xlarge T4**. Ships health + idle frame endpoints; LiveKit video publish + model runtime come later (Plan 01 Wave B/F).

| Item | Value |
|------|-------|
| Port | `8080` |
| Health | `GET /health` |
| Idle frame | `GET /idle/frame.jpg` |
| Arch | `linux/amd64` + NVIDIA GPU (prod) |
| Weights | S3 via `WEIGHTS_S3_URI` → `/models` |

## Build

```bash
docker build -f services/avatar/Dockerfile -t imjusthman/ai-live-avatar:dev .
```

## Run

```bash
docker run --rm -p 8080:8080 \
  -e WEIGHTS_S3_URI=s3://ai-livestream-dev/weights/avatar/ \
  imjusthman/ai-live-avatar:dev
```

Quick test:
```bash
curl -s http://localhost:8080/health
curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/idle/frame.jpg
```

## LiveKit publish integration (future — Plan 01 Wave B/F)

When the real avatar model replaces `health_app.py`, the pipeline is:

### 1. VideoSource setup

```python
from livekit import rtc

room = rtc.Room()
await room.connect(
    LIVEKIT_URL,
    rtc.RoomJoinToken(api_key=LK_API_KEY, api_secret=LK_API_SECRET, room="avatar")
)
source = rtc.VideoSource()
track = rtc.LocalVideoTrack.create_video_track("avatar-face", source)
await room.local_participant.publish_track(track, rtc.TrackPublishOptions(
    source=rtc.TrackSource.SOURCE_CAMERA,
    video_codec=rtc.VideoCodec.H264,
))
```

### 2. Idle loop (75 frames at 25 fps)

```python
import cv2
import numpy as np

idle_frame = np.full((480, 640, 3), (32, 128, 160), dtype=np.uint8)
for _ in range(75):
    # push one RGB frame to LiveKit
    frame = rtc.VideoFrame(
        width=640, height=480,
        buffer=rtc.VideoBuffer.from_ndarray(idle_frame, rtc.VideoBufferType.RGB),
        timestamp_us=int(time.time() * 1_000_000)
    )
    await source.capture_frame(frame)
    await asyncio.sleep(1 / 25)
```

### 3. LiveKit SDK version

```text
livekit>=0.18
```

- The idle loop plays before the model is ready (loading screen).
- `frame.jpg` endpoint is retired once LiveKit pushes start; the REST endpoint exists for ALB health / fallback proxy.
- Model runtime replaces `asyncio.sleep(1/25)` with inference-driven frame rate.

## Notes

- Multi-stage Python slim; non-root `appuser`.
- No weights in image layers.
- Replace `health_app.py` with real avatar runtime when models are ready.
