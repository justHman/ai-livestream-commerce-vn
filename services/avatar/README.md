# avatar — `justhman/ai-live-avatar`

Avatar-server skeleton for **g4dn.xlarge T4**. Ships a health endpoint; LiveKit video publish + model runtime come later (Plan 01 Wave B/F).

| Item | Value |
|------|-------|
| Port | `8080` |
| Health | `GET /health` |
| Arch | `linux/amd64` + NVIDIA GPU (prod) |
| Weights | S3 via `WEIGHTS_S3_URI` → `/models` |

## Build

```bash
docker build -f services/avatar/Dockerfile -t justhman/ai-live-avatar:dev .
```

## Run

```bash
docker run --rm -p 8080:8080 \
  -e WEIGHTS_S3_URI=s3://ai-livestream-dev/weights/avatar/ \
  justhman/ai-live-avatar:dev
```

## Notes

- Multi-stage Python slim; non-root `appuser`.
- No weights in image layers.
- Replace `health_app.py` with real avatar runtime when models are ready.
