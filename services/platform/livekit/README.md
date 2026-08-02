# livekit — `imjusthman/ai-live-livekit`

LiveKit SFU on Fargate Spot **ARM64**. Media UDP **50000–60000** public (SG).

| Item | Value |
|------|-------|
| Ports | `7880` (HTTP/WS), `7881` (RTC TCP), UDP `50000-60000` |
| Health | `GET /` |
| Arch | `linux/arm64` |
| Base | `livekit/livekit-server` |
| Weights | none |

## Build

```bash
docker build -f services/platform/livekit/Dockerfile -t imjusthman/ai-live-livekit:dev .
```

Prefer `--platform linux/arm64` for Fargate ARM.

## Run

```bash
docker run --rm -p 7880:7880 -p 7881:7881 \
  -e LIVEKIT_KEYS="devkey: devsecret" \
  imjusthman/ai-live-livekit:dev
```

## Notes

- Config baked at `/etc/livekit.yaml`; secrets via env/SSM, not image.
- Media UDP bypasses Cloudflare (direct to ENI) per aws-architecture.
