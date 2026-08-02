# syntax=docker/dockerfile:1.7
# justhman/ai-live-tts — vLLM-Omni / VieNeu-TTS-v2 TTS server, TTS ONLY.
# Fork: justHman/vllm-omni@feat/vieneu-tts-v0.22 (base tag v0.22.0).
# EC2 Spot g6 L4 24GB (shares GPU with LLM via NVIDIA_VISIBLE_DEVICES).
# Base = official vLLM image (torch + CUDA 12 runtime + vllm pre-built) — matches
# vllm-omni upstream docker/Dockerfile.cuda pattern. No manual torch/cuda install.
# Build from repo root:
#   docker build -f services/product/tts_service/Dockerfile -t imjusthman/ai-live-tts:dev .
# NEVER bake model weights — entrypoint syncs from WEIGHTS_S3_URI.

ARG BASE_IMAGE=vllm/vllm-openai:v0.22.1
ARG FORK_REPO=https://github.com/justHman/vllm-omni.git
ARG FORK_BRANCH=feat/vieneu-tts-v0.22
# Pin the exact fork commit (HEAD of feat/vieneu-tts-v0.22 as of 2026-07-24).
# The fork already contains the VieNeu adapter (vieneu.py + vieneu/pipeline.yaml).
# Do NOT pull upstream unstable; bump this pin only after verifying a new HEAD.
ARG FORK_COMMIT=e3d48e0a
ARG OMNI_TAG=v0.22.0

FROM ${BASE_IMAGE}

ARG FORK_REPO
ARG FORK_BRANCH
ARG FORK_COMMIT
ARG OMNI_TAG
# MODEL_ID is a LOCAL dir (vLLM 0.22 supports --model <local-dir> via Path.exists()).
# fetch_weights.sh syncs S3 → staging → validate → atomic publish to
# /models/vieneu before exec vllm-omni. Same root cause fix as LLM.
ENV PORT=8002 \
    WEIGHTS_LOCAL_DIR=/models \
    MODEL_SUBDIR=vieneu \
    GPU_MEMORY_UTILIZATION=0.35 \
    NVIDIA_VISIBLE_DEVICES=all \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=${OMNI_TAG} \
    PYTHONPATH="/app/src:/opt/vllm-omni:${PYTHONPATH:-}"

WORKDIR /app

# Clone fork, checkout pinned commit (not just the branch tip), editable install [vieneu].
# uv is pre-installed in the vllm base image; editable install lands in the
# base venv's site-packages so it persists into runtime (single-stage build).
# --branch + checkout FORK_COMMIT pins reproducibly without full history.
# awscli v2 binary (apt awscli v1 broken — KeyError 'opsworkscm').
RUN apt-get update \
    && apt-get install -y --no-install-recommends git jq curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsS "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip \
    && git clone --branch "${FORK_BRANCH}" "${FORK_REPO}" /opt/vllm-omni \
    && cd /opt/vllm-omni \
    && git fetch --tags --depth 1 origin "${OMNI_TAG}" \
    && git checkout "${FORK_COMMIT}" \
    && uv pip install --system --no-cache-dir -e "/opt/vllm-omni[vieneu]" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /models \
    && chown -R appuser:appuser /opt/vllm-omni /models

COPY services/product/tts_service/pyproject.toml services/product/tts_service/uv.lock /app/
COPY services/product/tts_service/src/tts/ /app/src/tts/
COPY services/scripts/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY services/product/tts_service/entrypoint.sh /entrypoint.sh
RUN uv pip install --system --no-cache-dir --no-deps /app \
    && chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8002

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=8 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec vllm serve \"${MODEL_ID}\" --omni --tokenizer \"${MODEL_ID}\" --config-format hf --host 0.0.0.0 --port \"${PORT}\" --dtype half --gpu-memory-utilization \"${GPU_MEMORY_UTILIZATION}\" --enforce-eager"]