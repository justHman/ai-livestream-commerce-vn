# syntax=docker/dockerfile:1.7
# justhman/ai-live-llm — vLLM OpenAI-compat server, LLM ONLY (Qwen3.5-4B-AWQ-4bit).
# EC2 Spot g6 L4 24GB. Base = official vLLM image (torch + CUDA 12 runtime + vllm
# pre-built; NO manual torch/cuda install — prod standard, matches vllm-omni upstream).
# Build from repo root:
#   docker build -f services/llm/Dockerfile -t imjusthman/ai-live-llm:dev .
# NEVER bake model weights — entrypoint syncs from WEIGHTS_S3_URI.

ARG BASE_IMAGE=vllm/vllm-openai:v0.22.0

FROM ${BASE_IMAGE}

# Runtime config. MODEL_ID downloaded at runtime to WEIGHTS_LOCAL_DIR (S3 sync).
ENV PORT=8001 \
    WEIGHTS_LOCAL_DIR=/models \
    MODEL_ID=cyankiwi/Qwen3.5-4B-AWQ-4bit \
    GPU_MEMORY_UTILIZATION=0.6 \
    ENABLE_PREFIX_CACHING=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1

WORKDIR /app

# awscli v2 for S3 weight sync (apt awscli v1 broken on some base images —
# KeyError 'opsworkscm' in awscli/customizations/opsworkscm.py). Install v2 binary.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsS "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip \
    && unzip -q /tmp/awscliv2.zip -d /tmp \
    && /tmp/aws/install \
    && rm -rf /tmp/aws /tmp/awscliv2.zip \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser \
    && mkdir -p /models \
    && chown appuser:appuser /models

COPY services/scripts/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY services/llm/entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=20s --timeout=5s --start-period=90s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sh", "-c", "exec vllm serve \"${MODEL_ID}\" --host 0.0.0.0 --port \"${PORT}\" --download-dir \"${WEIGHTS_LOCAL_DIR}\" --gpu-memory-utilization \"${GPU_MEMORY_UTILIZATION}\" $( [ \"${ENABLE_PREFIX_CACHING}\" = \"1\" ] && echo --enable-prefix-caching )"]