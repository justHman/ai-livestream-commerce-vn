# syntax=docker/dockerfile:1.7
# justhman/ai-live-tts — provider-neutral TTS serving (VieNeu v3 Turbo default).
# Build from repo root:
#   docker build -f services/product/tts_service/Dockerfile -t imjusthman/ai-live-tts:dev .
# GPU build (VieNeu pytorch backend):
#   docker build --build-arg WITH_CUDA=1 -f services/product/tts_service/Dockerfile -t imjusthman/ai-live-tts:dev .
# CPU-only (TTS_ACCELERATOR=cpu, ONNX backend): omit WITH_CUDA.
# Runtime requires nvidia-container-toolkit + NVIDIA_VISIBLE_DEVICES (GPU mode).
# Weights are NEVER baked: fetch_weights.sh syncs from WEIGHTS_S3_URI at boot.

ARG PYTHON_VERSION=3.11

# ── deps stage ────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python${PYTHON_VERSION}-bookworm-slim AS deps

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# With CUDA, the lock's torch wheel (CPU build) is replaced by the CUDA build
# from the PyTorch index, matching the pinned 2.13.0 version in the lock.
ARG WITH_CUDA=0
ENV UV_INDEX_URL=${UV_INDEX_URL:-https://pypi.org/simple} \
    UV_EXTRA_INDEX_URL=${UV_EXTRA_INDEX_URL:-}

WORKDIR /build

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential git \
    && rm -rf /var/lib/apt/lists/*

COPY services/product/tts_service/pyproject.toml services/product/tts_service/uv.lock services/product/tts_service/README.md ./
COPY services/product/tts_service/src/tts/ ./src/tts/
RUN if [ "$WITH_CUDA" = "1" ]; then \
      UV_EXTRA_INDEX_URL="https://download.pytorch.org/whl/cu126 ${UV_EXTRA_INDEX_URL}" \
        uv sync --frozen --no-dev --extra vieneu --override 'torch==2.13.0+cu126' --index-strategy unsafe-best-match; \
    else \
      uv sync --frozen --no-dev --extra vieneu; \
    fi

# ── runtime ───────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    APP_HOME=/app \
    PORT=8002 \
    PATH="/opt/venv/bin:${PATH}" \
    PYTHONPATH="/app/services/product/tts_service/src:/app" \
    TTS_PROVIDER=vieneu_v3 \
    TTS_MODEL_REVISION=pnnbao-ump/VieNeu-TTS-v3-Turbo \
    TTS_ACCELERATOR=auto \
    NVIDIA_VISIBLE_DEVICES=all

WORKDIR ${APP_HOME}

# curl for HEALTHCHECK; git for the vieneu SDK's weight fetch at first boot.
# NVIDIA: the container runtime (nvidia-container-toolkit) must be installed
# on the host and the task must request a GPU; the image itself carries no
# CUDA runtime — torch wheels ship the needed libraries.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

COPY --from=deps --chown=appuser:appuser /opt/venv /opt/venv

COPY --chown=appuser:appuser services/product/tts_service/src/tts/ ./services/product/tts_service/src/tts/
COPY --chown=appuser:appuser scripts/model_assets/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY --chown=appuser:appuser services/product/tts_service/entrypoint.sh /entrypoint.sh

RUN chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh

USER appuser

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=8 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "tts.main:app", "--host", "0.0.0.0", "--port", "8002"]
