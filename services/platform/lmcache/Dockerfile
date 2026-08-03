# syntax=docker/dockerfile:1.7
# justhman/ai-live-lmcache — pinned upstream LMCache standalone MP server.
# Build from repo root:
#   docker build -f services/platform/lmcache/Dockerfile -t justhman/ai-live-lmcache:dev .
# Baseline: python:3.11-slim (lmcache 0.5.2 ships cp311 manylinux x86_64 wheels).
#   python:3.11.9-alpine3.20 not used — lmcache needs glibc wheels.
# LMCache upstream (lmcache 0.5.2, 2026-07-30) exposes the standalone MP server
# as `lmcache server` (ZMQ tcp://:5555 + HTTP frontend :8080 with real
# /healthcheck and /metrics). No synthetic metrics/fallback/best-effort exists.

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
# Immutable pin: lmcache==0.5.2 (PyPI 2026-07-30, requires_python >=3.10,<3.14).
# No `|| ...` — missing binary/package/build capability FAILs the build.
RUN pip install --no-cache-dir \
    "lmcache==0.5.2" \
    "uvicorn[standard]>=0.27" \
    "prometheus-client>=0.20"

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT_HTTP=8080 \
    PORT_ZMQ=5555 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY services/platform/lmcache/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && chown -R appuser:appuser ${APP_HOME}

USER appuser

EXPOSE 5555 8080

# Real upstream health/metrics contract:
#   GET /healthcheck -> 503 until engine init, 200 when ready
#   GET /metrics     -> Prometheus text
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT_HTTP}/healthcheck" || exit 1

ENTRYPOINT ["/entrypoint.sh"]