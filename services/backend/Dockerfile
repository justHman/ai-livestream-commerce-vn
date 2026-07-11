# syntax=docker/dockerfile:1.7
# justhman/ai-live-backend — FastAPI control plane (Fargate Spot ARM64).
# Build from repo root:
#   docker build -f services/backend/Dockerfile -t justhman/ai-live-backend:dev .
# Weights: none (stateless API).

ARG PYTHON_VERSION=3.11

# ── deps stage ────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS deps

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Prefer root pyproject when present; fall back to a minimal pin set for CI skeleton.
COPY pyproject.toml* ./
RUN if [ -f pyproject.toml ]; then \
      pip install --upgrade pip \
      && pip install .; \
    else \
      pip install --upgrade pip \
      && pip install \
        "fastapi>=0.110" \
        "uvicorn[standard]>=0.27" \
        "pydantic>=2.6" \
        "httpx>=0.27" \
        "python-multipart>=0.0.9" \
        "redis>=5.0"; \
    fi

# ── runtime ───────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=8800

WORKDIR ${APP_HOME}

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --from=deps /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=deps /usr/local/bin /usr/local/bin

# Build context = repo root. core/ is the application package.
COPY core/ ./core/
COPY services/backend/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && chown -R appuser:appuser ${APP_HOME}

USER appuser

EXPOSE 8800

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/v1/health/live" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "core.server:app", "--host", "0.0.0.0", "--port", "8800"]
