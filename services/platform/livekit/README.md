# livekit

Local/sandbox LiveKit SFU wrapper around the **immutably pinned** official
upstream image.

| Item | Value |
|------|-------|
| Base | `livekit/livekit-server:v1.13.5@sha256:3497163e…06b1` (verified 2026-08-03) |
| Ports | `7880` (HTTP/WS), `7881` (RTC TCP), UDP `50000-60000` |
| Health | `GET /` (406 until node heartbeat fresh; 200 when ready) |
| Credentials | `LIVEKIT_API_KEY` + `LIVEKIT_API_SECRET` (fail startup when missing) |

## Purpose and topology

This directory owns the **local/sandbox** wrapper only. It does **not** deploy
LiveKit to AWS. The previous Fargate Spot service/task topology in
`infra/modules/compute` was removed: dev/staging/prod clients use **LiveKit
Cloud** via `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, or SSM
references. A future self-hosted cloud requires a separate VM design; there is
no replacement self-host topology in this repo.

## Build

```bash
docker build -f services/platform/livekit/Dockerfile -t imjusthman/ai-live-livekit:dev .
```

## Run (local sandbox)

```bash
docker run --rm -p 7880:7880 -p 7881:7881 \
  -e LIVEKIT_API_KEY=devkey \
  -e LIVEKIT_API_SECRET=$(openssl rand -base64 32) \
  imjusthman/ai-live-livekit:dev
```

Missing either credential fails startup — no empty-keys container.

## Validation (no Docker required)

```bash
python services/platform/livekit/validate_config.py   # pin + YAML + entrypoint contract
```

## Real-process smoke (requires a running local endpoint)

```bash
LIVEKIT_API_KEY=devkey LIVEKIT_API_SECRET=... \
  python services/platform/livekit/smoke.py            # default http://127.0.0.1:7880
```

The smoke asserts the real host responds 200 (fresh node heartbeat = real
signaling readiness), not a placeholder listener.

## Notes

- SDK clients outside local use LiveKit Cloud variables/SSM refs; the backend
  `LIVEKIT_URL`/key/secret env come from SSM, never the image.
- Media UDP bypasses Cloudflare (direct to ENI) per aws-architecture.