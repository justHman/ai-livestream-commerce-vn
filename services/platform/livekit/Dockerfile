# syntax=docker/dockerfile:1.7
# justhman/ai-live-livekit — LiveKit SFU (Fargate Spot ARM64).
# Build from repo root:
#   docker build -f services/platform/livekit/Dockerfile -t justhman/ai-live-livekit:dev .
# Thin wrapper around official image + config mount path.

FROM livekit/livekit-server:latest

ENV LIVEKIT_CONFIG=/etc/livekit.yaml \
    PORT=7880

# Official image is already non-root-friendly; add curl for HEALTHCHECK if missing.
USER root
RUN if ! command -v curl >/dev/null 2>&1; then \
      (apk add --no-cache curl 2>/dev/null || apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*) || true; \
    fi
COPY services/platform/livekit/livekit.yaml /etc/livekit.yaml
COPY services/platform/livekit/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
USER 1000

EXPOSE 7880 7881
# UDP media range is opened at the SG / task networking layer (50000-60000).

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:7880/" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--config", "/etc/livekit.yaml"]
