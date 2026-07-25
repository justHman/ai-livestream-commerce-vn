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
# Qwen/Qwen3-4B-AWQ (arch Qwen3ForCausalLM, model_type qwen3) — vLLM 0.22 supports
# Qwen3ForCausalLM natively. cyankiwi/Qwen3.5-4B-AWQ-4bit uses a custom fork
# arch (Qwen3_5ForConditionalGeneration) that vLLM 0.22 model registry cannot
# inspect -> subprocess timeout -> container crash.
ENV PORT=8001 \
    WEIGHTS_LOCAL_DIR=/models \
    MODEL_ID=Qwen/Qwen3-4B-AWQ \
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
    && rm /tmp/gen_vllm_modelinfo_cache.py /tmp/modelinfos

RUN chmod +x /usr/local/bin/fetch_weights.sh /entrypoint.sh \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=10s --start-period=300s --retries=8 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
# --enforce-eager: skip CUDA graph capture (faster init, less VRAM, avoids
# subprocess inspect path that hangs on L4 + receives SIGINT from ECS during
# long init). vLLM 0.22 `serve` does NOT support --task generate (newer arg).
CMD ["sh", "-c", "exec vllm serve \"${MODEL_ID}\" --host 0.0.0.0 --port \"${PORT}\" --download-dir \"${WEIGHTS_LOCAL_DIR}\" --gpu-memory-utilization \"${GPU_MEMORY_UTILIZATION}\" --enforce-eager $( [ \"${ENABLE_PREFIX_CACHING}\" = \"1\" ] && echo --enable-prefix-caching )"]