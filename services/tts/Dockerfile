# syntax=docker/dockerfile:1.7
# justhman/ai-live-tts — vLLM-Omni / VieNeu TTS (shares g6 GPU with LLM).
# Build from repo root:
#   docker build -f services/tts/Dockerfile -t justhman/ai-live-tts:dev .
# NEVER bake model weights — entrypoint syncs from WEIGHTS_S3_URI.

ARG CUDA_IMAGE=nvidia/cuda:12.4.1-runtime-ubuntu22.04

FROM ${CUDA_IMAGE} AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8002 \
    WEIGHTS_LOCAL_DIR=/models \
    MODEL_ID=pnnbao-ump/VieNeu-TTS-v2 \
    GPU_MEMORY_UTILIZATION=0.25 \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         python3 python3-pip python3-venv curl ca-certificates \
         awscli \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/bin/python

# Production: install vLLM-Omni fork with [vieneu] extra from justHman/vllm-omni.
# Skeleton keeps image buildable without private wheelhouse.
RUN pip3 install --no-cache-dir --upgrade pip \
    && pip3 install --no-cache-dir "vllm>=0.6.0" || \
       echo "[tts] WARN: vllm/omni install skipped/failed — bake on GPU CI runner"

COPY services/scripts/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY services/tts/entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && mkdir -p /models

EXPOSE 8002

HEALTHCHECK --interval=20s --timeout=5s --start-period=90s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
# Documented prod CMD (vLLM-Omni serve). Replace with fork binary when image is finalized.
CMD ["sh", "-c", "exec vllm serve \"${MODEL_ID}\" --host 0.0.0.0 --port \"${PORT}\" --download-dir \"${WEIGHTS_LOCAL_DIR}\" --gpu-memory-utilization \"${GPU_MEMORY_UTILIZATION}\""]
