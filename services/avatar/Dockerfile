# syntax=docker/dockerfile:1.7
# justhman/ai-live-avatar — avatar-server (g4dn T4). Skeleton: health + optional weights.
# Build from repo root:
#   docker build -f services/avatar/Dockerfile -t justhman/ai-live-avatar:dev .
# NEVER bake model weights — entrypoint syncs from WEIGHTS_S3_URI.

ARG PYTHON_VERSION=3.11

FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN pip install --upgrade pip \
    && pip install "fastapi>=0.110" "uvicorn[standard]>=0.27" "pydantic>=2.6" "pillow>=10.0"

FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    WEIGHTS_LOCAL_DIR=/models \
    APP_HOME=/app

WORKDIR ${APP_HOME}

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates awscli \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /models \
    && chown appuser:appuser /models

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Minimal health app until LiveKit publish + model runtime lands.
COPY services/avatar/health_app.py ./health_app.py
COPY services/scripts/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY services/avatar/entrypoint.sh /entrypoint.sh

RUN chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && chown -R appuser:appuser ${APP_HOME}

USER appuser

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "health_app:app", "--host", "0.0.0.0", "--port", "8080"]
