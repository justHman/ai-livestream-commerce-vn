# syntax=docker/dockerfile:1.7
# justhman/ai-live-lmcache — LMCache server (c7g.2xlarge Spot ARM, stateful KV).
# Build from repo root:
#   docker build -f services/platform/lmcache/Dockerfile -t justhman/ai-live-lmcache:dev .
# No model weights; warm KV lives in process RAM (desired_count toggled by LMCACHE_ENABLED).

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN pip install --upgrade pip \
    && pip install "fastapi>=0.110" "uvicorn[standard]>=0.27" "pydantic>=2.6" \
    && (pip install "lmcache>=0.2.0" || echo "[lmcache] package optional at skeleton time")

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT_METRICS=8080 \
    PORT_ZMQ=5555 \
    APP_HOME=/app

WORKDIR ${APP_HOME}

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

COPY services/platform/lmcache/metrics_app.py ./metrics_app.py
COPY services/platform/lmcache/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh \
    && chown -R appuser:appuser ${APP_HOME}

USER appuser

EXPOSE 5555 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT_METRICS}/metrics" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
# Skeleton: metrics HTTP. When lmcache CLI is available, entrypoint prefers it.
CMD ["uvicorn", "metrics_app:app", "--host", "0.0.0.0", "--port", "8080"]
