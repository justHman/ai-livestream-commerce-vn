# syntax=docker/dockerfile:1.7
# justhman/ai-live-livekit — LiveKit SFU thin wrapper.
# Build from repo root:
#   docker build -f services/platform/livekit/Dockerfile -t justhman/ai-live-livekit:dev .
# Immutably pinned upstream runtime (no mutable fallback tag), verified against
# Docker Hub registry metadata on 2026-08-03:
#   livekit/livekit-server:v1.13.5
#   digest sha256:3497163e15c48fef6e7830c1ce78716f9e9d5edc28abf7aa90b61c86e93bbc306b1
#   published 2026-07-31T07:18:19Z (source: hub.docker.com/v2/repositories/livekit/livekit-server)

FROM livekit/livekit-server:v1.13.5@sha256:3497163e15c48fef6e7830c1ce78716f9e9d5edc28abf7aa90b61c86e93bbc306b1

ENV LIVEKIT_CONFIG=/etc/livekit.yaml \
    PORT=7880

# Config + entrypoint only. No package install: the official image already
# ships curl and the livekit-server binary; any install that does not run must
# fail the build instead of being ignored.
COPY services/platform/livekit/livekit.yaml /etc/livekit.yaml
COPY services/platform/livekit/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 7880 7881
# UDP media range is opened at the SG / networking layer (50000-60000).

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:7880/" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["--config", "/etc/livekit.yaml"]