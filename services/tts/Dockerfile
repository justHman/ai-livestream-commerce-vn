# syntax=docker/dockerfile:1.7
# justhman/ai-live-tts — vLLM-Omni / VieNeu-TTS-v2 TTS server, TTS ONLY.
# Fork: justHman/vllm-omni@feat/vieneu-tts-v0.22 (base tag v0.22.0).
# EC2 Spot g6 L4 24GB (shares GPU with LLM via NVIDIA_VISIBLE_DEVICES).
# Base = official vLLM image (torch + CUDA 12 runtime + vllm pre-built) — matches
# vllm-omni upstream docker/Dockerfile.cuda pattern. No manual torch/cuda install.
# Build from repo root:
#   docker build -f services/tts/Dockerfile -t imjusthman/ai-live-tts:dev .
# NEVER bake model weights — entrypoint syncs from WEIGHTS_S3_URI.

ARG BASE_IMAGE=vllm/vllm-openai:v0.22.0
ARG FORK_REPO=https://github.com/justHman/vllm-omni.git
ARG FORK_BRANCH=feat/vieneu-tts-v0.22
ARG OMNI_TAG=v0.22.0

FROM ${BASE_IMAGE}

ARG FORK_REPO
ARG FORK_BRANCH
ARG OMNI_TAG
ENV PORT=8002 \
    WEIGHTS_LOCAL_DIR=/models \
    MODEL_ID=pnnbao-ump/VieNeu-TTS-v2 \
    GPU_MEMORY_UTILIZATION=0.25 \
    NVIDIA_VISIBLE_DEVICES=all \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION=${OMNI_TAG} \
    PYTHONPATH="/opt/vllm-omni:${PYTHONPATH:-}"

WORKDIR /app

# Clone fork (shallow, tags for setuptools-scm) + editable install [vieneu].
# uv is pre-installed in the vllm base image; editable install lands in the
# base venv's site-packages so it persists into runtime (single-stage build).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git jq awscli \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --depth 1 --branch "${FORK_BRANCH}" "${FORK_REPO}" /opt/vllm-omni \
    && cd /opt/vllm-omni \
    && git fetch --tags --depth 1 origin "${OMNI_TAG}" \
    && uv pip install --no-cache-dir -e "/opt/vllm-omni[vieneu]" \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /models \
    && chown -R appuser:appuser /opt/vllm-omni /models

COPY services/scripts/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY services/tts/entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8002

HEALTHCHECK --interval=20s --timeout=5s --start-period=120s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec vllm serve \"${MODEL_ID}\" --omni --host 0.0.0.0 --port \"${PORT}\" --download-dir \"${WEIGHTS_LOCAL_DIR}\" --dtype half --gpu-memory-utilization \"${GPU_MEMORY_UTILIZATION}\""]