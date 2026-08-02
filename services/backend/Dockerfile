# syntax=docker/dockerfile:1.7
# justhman/ai-live-backend — FastAPI control plane (Fargate Spot ARM64).
# Build from repo root:
#   docker build --platform linux/arm64 -f services/backend/Dockerfile -t imjusthman/ai-live-backend:dev .
# Weights: none (stateless API). Uses uv for fast, locked dependency resolution.

ARG PYTHON_VERSION=3.11

# ── deps stage ────────────────────────────────────────────────
# uv ships its own Python; we install into the slim image's interpreter (--system).
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS deps

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /build

# build-essential only if any wheel needs to compile (numpy/Pillow ship wheels — usually skip).
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Resolve and install the project + its runtime deps into /opt/venv.
# uv reads pyproject.toml; --extra test omitted (runtime only).
COPY pyproject.toml ./
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python \
        "fastapi" "uvicorn[standard]" "pydantic" "httpx" \
        "python-multipart" "python-dotenv" "redis" "PyJWT" \
        "numpy" "Pillow" "requests" "websocket-client" "sentence-transformers>=5,<6"

# ── runtime ───────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=8800 \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR ${APP_HOME}

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

# Copy the isolated venv built by uv (smaller than site-packages sprawl).
COPY --from=deps --chown=appuser:appuser /opt/venv /opt/venv

# Build context = repo root. core/ is the application package.
# providers/ is imported by core/render/cloud.py (cloud render backend).
COPY --chown=appuser:appuser core/ ./core/
COPY --chown=appuser:appuser providers/ ./providers/
COPY --chown=appuser:appuser services/backend/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

USER appuser

EXPOSE 8800

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/v1/health/live" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "core.server:app", "--host", "0.0.0.0", "--port", "8800"]
