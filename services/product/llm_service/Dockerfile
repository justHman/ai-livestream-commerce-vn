# syntax=docker/dockerfile:1.7
# justhman/ai-live-llm — vLLM OpenAI-compat server, LLM ONLY (Qwen3.5-4B-AWQ-4bit).
# EC2 Spot g6 L4 24GB. Base = official vLLM image (torch + CUDA 12 runtime + vllm
# pre-built; NO manual torch/cuda install — prod standard, matches vllm-omni upstream).
# Build from repo root:
#   docker build -f services/product/llm_service/Dockerfile -t imjusthman/ai-live-llm:dev .
# NEVER bake model weights — entrypoint syncs from WEIGHTS_S3_URI.

ARG BASE_IMAGE=vllm/vllm-openai:v0.22.1

FROM ${BASE_IMAGE}

# Runtime config. MODEL_ID is a LOCAL dir (vLLM 0.22 supports --model <local-dir>
# via Path.exists() — root cause fix: path must exist + contain config.json at
# resolve time, no HF repo ID, no phone home). fetch_weights.sh syncs S3 →
# staging → validate → atomic publish to /models/qwen3-4b-awq before exec vllm.
ENV PORT=8001 \
    WEIGHTS_LOCAL_DIR=/models \
    MODEL_SUBDIR=qwen3-4b-awq \
    GPU_MEMORY_UTILIZATION=0.6 \
    ENABLE_PREFIX_CACHING=1 \
    HF_HUB_ENABLE_HF_TRANSFER=1 \
    PYTHONPATH=/app/src

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

COPY services/product/llm_service/pyproject.toml services/product/llm_service/uv.lock /app/
COPY services/product/llm_service/src/llm/ /app/src/llm/
COPY services/scripts/fetch_weights.sh /usr/local/bin/fetch_weights.sh
COPY services/product/llm_service/entrypoint.sh /entrypoint.sh
COPY scripts/gen_vllm_modelinfo_cache.py /tmp/gen_vllm_modelinfo_cache.py

# Pre-generate _ModelInfo cache (PR #23558, already merged in vLLM 0.22.0).
# vLLM reads $VLLM_CACHE_ROOT/modelinfos/{module}-{class}.json and skips
# _run_in_subprocess on cache hit. The subprocess hangs importing
# torch+CUDA on L4 (~10s) + receives SIGINT from ECS during long init
# -> KeyboardInterrupt -> exit 1. Cache hit = 0.6s, no SIGINT.
# Bake into appuser HOME (task runs as appuser uid 10001), NOT /root.
RUN python3 /tmp/gen_vllm_modelinfo_cache.py --out /tmp/modelinfos \
    && mkdir -p /home/appuser/.cache/vllm/modelinfos \
    && cp /tmp/modelinfos/*.json /home/appuser/.cache/vllm/modelinfos/ \
    && chown -R appuser:appuser /home/appuser/.cache \
    && rm -rf /tmp/gen_vllm_modelinfo_cache.py /tmp/modelinfos

RUN UV_PROJECT_ENVIRONMENT=/usr/local uv sync --frozen --no-dev --inexact \
    && chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=8 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
# --enforce-eager: skip CUDA graph capture (faster init, less VRAM, avoids
# subprocess inspect path that hangs on L4 + receives SIGINT from ECS during
# long init). vLLM 0.22 `serve` does NOT support --task generate (newer arg).
CMD ["sh", "-c", "exec vllm serve \"${MODEL_ID}\" --tokenizer \"${MODEL_ID}\" --config-format hf --model-impl auto --served-model-name qwen3-4b-awq --host 0.0.0.0 --port \"${PORT}\" --gpu-memory-utilization \"${GPU_MEMORY_UTILIZATION}\" --enforce-eager --quantization awq_marlin $( [ \"${ENABLE_PREFIX_CACHING}\" = \"1\" ] && echo --enable-prefix-caching )"]