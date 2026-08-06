# syntax=docker/dockerfile:1.7
# justhman/ai-live-backend — FastAPI control plane (Fargate Spot ARM64).
# Build from repo root:
#   docker build --platform linux/arm64 -f services/product/backend_service/Dockerfile -t imjusthman/ai-live-backend:dev .
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

# Install exactly the service-owned lock; root tooling metadata is never used.
# Canonical siblings (llm/tts/avatar) must exist for uv's `[tool.uv.sources]`
# path deps (`../llm_service`) to resolve — copy metadata into /llm_service etc.
COPY services/product/backend_service/pyproject.toml services/product/backend_service/uv.lock services/product/backend_service/README.md ./
COPY services/product/backend_service/src/backend/ ./src/backend/
# Path deps build wheels from their project dir: README (pyproject readme field)
# + src/ layout are required, not just lock metadata.
COPY services/product/llm_service/pyproject.toml services/product/llm_service/uv.lock services/product/llm_service/README.md /llm_service/
COPY services/product/llm_service/src/llm/ /llm_service/src/llm/
COPY services/product/tts_service/pyproject.toml services/product/tts_service/uv.lock services/product/tts_service/README.md /tts_service/
COPY services/product/tts_service/src/tts/ /tts_service/src/tts/
COPY services/product/avatar_service/pyproject.toml services/product/avatar_service/uv.lock services/product/avatar_service/README.md /avatar_service/
COPY services/product/avatar_service/src/avatar/ /avatar_service/src/avatar/
RUN uv sync --frozen --no-dev --extra director --extra livekit --extra postgres

# ── runtime ───────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=8800 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app/services/product/backend_service/src:/app/services/product/llm_service/src:/app/services/product/tts_service/src:/app/services/product/avatar_service/src:/app"

WORKDIR ${APP_HOME}

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

# Copy the isolated venv built by uv (smaller than site-packages sprawl).
COPY --from=deps --chown=appuser:appuser /opt/venv /opt/venv

# Canonical package plus sibling self-host sources imported by the seams.
COPY --chown=appuser:appuser services/product/backend_service/src/backend/ ./services/product/backend_service/src/backend/
COPY --chown=appuser:appuser services/product/llm_service/src/llm/ ./services/product/llm_service/src/llm/
COPY --chown=appuser:appuser services/product/tts_service/src/tts/ ./services/product/tts_service/src/tts/
COPY --chown=appuser:appuser services/product/avatar_service/src/avatar/ ./services/product/avatar_service/src/avatar/
COPY --chown=appuser:appuser services/product/backend_service/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh

USER appuser

EXPOSE 8800

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/api/v1/health/live" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8800"]
