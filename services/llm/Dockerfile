# syntax=docker/dockerfile:1.7
# justhman/ai-live-llm — vLLM OpenAI-compat server (EC2 Spot g6 L4).
# Build from repo root:
#   docker build -f services/llm/Dockerfile -t justhman/ai-live-llm:dev .
# NEVER bake model weights — entrypoint syncs from WEIGHTS_S3_URI.

ARG CUDA_IMAGE=nvidia/cuda:12.4.1-runtime-ubuntu22.04

FROM ${CUDA_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8001 \
    WEIGHTS_LOCAL_DIR=/models \
    MODEL_ID=cyankiwi/Qwen3.5-4B-AWQ-4bit \
    GPU_MEMORY_UTILIZATION=0.6 \
    ENABLE_PREFIX_CACHING=1 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-pip python3-venv curl ca-certificates \
         awscli \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# vLLM is large; pin loosely so CI can still parse the Dockerfile without a GPU build.
# Production images should rebuild with an explicit vllm version lock.
RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir "vllm>=0.6.0" || \
       echo "[llm] WARN: vllm install skipped/failed in this build env — bake on GPU CI runner"

COPY services/scripts/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY services/llm/entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && mkdir -p /models

EXPOSE 8001

HEALTHCHECK --interval=20s --timeout=5s --start-period=90s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
# Documented prod CMD (vLLM OpenAI server). Overridden only for local smoke stubs.
CMD ["sh", "-c", "exec vllm serve \"${MODEL_ID}\" --host 0.0.0.0 --port \"${PORT}\" --download-dir \"${WEIGHTS_LOCAL_DIR}\" --gpu-memory-utilization \"${GPU_MEMORY_UTILIZATION}\" $( [ \"${ENABLE_PREFIX_CACHING}\" = \"1\" ] && echo --enable-prefix-caching )"]
