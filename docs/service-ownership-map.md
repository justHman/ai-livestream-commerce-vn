# Service ownership map

This is the pre-migration inventory for OpenSpec Task 1.1. It classifies every
runtime-bearing legacy root from commit `c73880a` without moving or deleting it.
A `source` directory includes all files below it unless a narrower row overrides
one of those files. `target` is the canonical owner path for later tasks, not a
path created by this task.

<!-- service-ownership-map:start -->
```json
{
  "mappings": [
    {"category": "product/backend", "source": "core/server.py", "target": "services/product/backend_service/src/backend/main.py"},
    {"category": "product/backend", "source": "core/api/", "target": "services/product/backend_service/src/backend/api/"},
    {"category": "product/backend", "source": "core/config.py", "target": "services/product/backend_service/src/backend/config.py"},
    {"category": "product/backend", "source": "core/db/", "target": "services/product/backend_service/src/backend/db/"},
    {"category": "product/backend", "source": "core/sql/runtime_schema.sql", "target": "services/product/backend_service/src/backend/db/sql/runtime_schema.sql"},
    {"category": "product/backend", "source": "core/store.py", "target": "services/product/backend_service/src/backend/db/session_store.py"},
    {"category": "product/backend", "source": "core/director/", "target": "services/product/backend_service/src/backend/application/director/"},
    {"category": "product/backend", "source": "core/schemas/", "target": "services/product/backend_service/src/backend/application/schemas/"},
    {"category": "product/backend", "source": "core/stream/chunker.py", "target": "services/product/backend_service/src/backend/application/text_chunker.py"},
    {"category": "product/backend", "source": "core/engine_manager.py", "target": "services/product/backend_service/src/backend/bootstrap/container.py"},
    {"category": "product/backend", "source": "core/livekit_tokens.py", "target": "services/product/backend_service/src/backend/application/clients/livekit.py"},
    {"category": "product/backend", "source": "core/llm/adapters/openai_compat.py", "target": "services/product/backend_service/src/backend/application/clients/llm/openai_compatible.py"},
    {"category": "product/backend", "source": "core/tts/adapters/elevenlabs.py", "target": "services/product/backend_service/src/backend/application/clients/tts/elevenlabs.py"},
    {"category": "product/backend", "source": "core/tts/adapters/openai_speech.py", "target": "services/product/backend_service/src/backend/application/clients/tts/openai_speech.py"},
    {"category": "product/backend", "source": "core/tts/adapters/remote_http.py", "target": "services/product/backend_service/src/backend/application/clients/tts/self_hosted.py"},
    {"category": "product/backend", "source": "core/render/cloud.py", "target": "services/product/backend_service/src/backend/application/clients/avatar/liveavatar.py"},
    {"category": "product/backend", "source": "core/render/remote_avatar.py", "target": "services/product/backend_service/src/backend/application/clients/avatar/baidu_xiling.py"},
    {"category": "product/backend", "source": "providers/liveavatar_cloud/sdk/", "target": "services/product/backend_service/src/backend/application/clients/avatar/liveavatar.py"},
    {"category": "product/backend", "source": "providers/liveavatar_cloud/service/server.py", "target": "services/product/backend_service/src/backend/application/clients/avatar/liveavatar.py"},
    {"category": "support/sandbox", "source": "providers/liveavatar_cloud/service/__init__.py", "target": "tests/sandbox/liveavatar/__init__.py"},
    {"category": "support/sandbox", "source": "providers/liveavatar_cloud/service/conversation.py", "target": "tests/sandbox/liveavatar/conversation.py"},
    {"category": "support/sandbox", "source": "providers/liveavatar_cloud/service/lite_agent.py", "target": "tests/sandbox/liveavatar/lite_agent.py"},
    {"category": "support/sandbox", "source": "providers/liveavatar_cloud/service/store.py", "target": "tests/sandbox/liveavatar/store.py"},
    {"category": "support/sandbox", "source": "providers/liveavatar_cloud/service/colab_server.py", "target": "tests/sandbox/liveavatar/colab_server.py"},

    {"category": "product/llm", "source": "core/llm/__init__.py", "target": "services/product/llm_service/src/llm/__init__.py"},
    {"category": "product/llm", "source": "core/llm/base.py", "target": "services/product/llm_service/src/llm/engines/base.py"},
    {"category": "product/llm", "source": "core/llm/adapters/__init__.py", "target": "services/product/llm_service/src/llm/engines/__init__.py"},
    {"category": "product/llm", "source": "core/llm/adapters/llamacpp.py", "target": "services/product/llm_service/src/llm/engines/llamacpp.py"},
    {"category": "product/llm", "source": "core/llm/adapters/sglang.py", "target": "services/product/llm_service/src/llm/engines/sglang.py"},
    {"category": "product/llm", "source": "core/llm/adapters/transformers.py", "target": "services/product/llm_service/src/llm/engines/transformers.py"},
    {"category": "product/llm", "source": "core/llm/adapters/vllm.py", "target": "services/product/llm_service/src/llm/engines/vllm.py"},
    {"category": "product/llm", "source": "services/llm/", "target": "services/product/llm_service/"},

    {"category": "product/tts", "source": "core/tts/__init__.py", "target": "services/product/tts_service/src/tts/__init__.py"},
    {"category": "product/tts", "source": "core/tts/base.py", "target": "services/product/tts_service/src/tts/engines/base.py"},
    {"category": "product/tts", "source": "core/tts/adapters/__init__.py", "target": "services/product/tts_service/src/tts/engines/__init__.py"},
    {"category": "product/tts", "source": "core/tts/adapters/transformers.py", "target": "services/product/tts_service/src/tts/engines/transformers.py"},
    {"category": "product/tts", "source": "core/tts/adapters/vieneu.py", "target": "services/product/tts_service/src/tts/engines/vieneu.py"},
    {"category": "product/tts", "source": "core/tts/adapters/cosyvoice.py", "target": "services/product/tts_service/src/tts/engines/cosyvoice.py"},
    {"category": "product/tts", "source": "services/tts/", "target": "services/product/tts_service/"},

    {"category": "product/avatar", "source": "core/render/base.py", "target": "services/product/avatar_service/src/avatar/engines/base.py"},
    {"category": "product/avatar", "source": "core/render/self_host.py", "target": "services/product/avatar_service/src/avatar/engines/avatarforcing.py"},
    {"category": "product/avatar", "source": "core/render/locks.py", "target": "services/product/avatar_service/src/avatar/sessions.py"},
    {"category": "product/avatar", "source": "core/render/mock.py", "target": "services/product/avatar_service/src/avatar/engines/avatarforcing.py"},
    {"category": "product/avatar", "source": "core/render/orchestrator.py", "target": "services/product/avatar_service/src/avatar/sessions.py"},
    {"category": "product/avatar", "source": "core/render/queue.py", "target": "services/product/avatar_service/src/avatar/sessions.py"},
    {"category": "product/avatar", "source": "core/render/windows.py", "target": "services/product/avatar_service/src/avatar/engines/avatarforcing.py"},
    {"category": "product/avatar", "source": "core/livekit_publish.py", "target": "services/product/avatar_service/src/avatar/publishing/livekit.py"},
    {"category": "product/avatar", "source": "core/pipecat_bridge.py", "target": "services/product/avatar_service/src/avatar/sessions.py"},
    {"category": "product/avatar", "source": "services/avatar/", "target": "services/product/avatar_service/"},

    {"category": "platform/livekit", "source": "services/livekit/", "target": "services/platform/livekit/"},
    {"category": "platform/lmcache", "source": "services/lmcache/", "target": "services/platform/lmcache/"},
    {"category": "platform/postgres", "source": "infra/modules/database/", "target": "infra/modules/database/ (retained; referenced by services/platform/postgres/)"},
    {"category": "platform/redis", "source": "infra/modules/database/", "target": "infra/modules/database/ (retained; referenced by services/platform/redis/)"},

    {"category": "product/backend", "source": "services/backend/", "target": "services/product/backend_service/"},
    {"category": "support/workbench", "source": "frontend/", "target": "workbench/"},
    {"category": "support/workbench", "source": "core/debug/", "target": "workbench/src/{fixtures,simulator}.ts and workbench/scripts/smoke_test.py"},
    {"category": "support/tests", "source": "core/tests/", "target": "services/product/*_service/tests/ and tests/{e2e,sandbox}/"},
    {"category": "support/benchmarks", "source": "scripts/bench_api.py (removed 1.58; moved to benchmarks/api/latency.py)", "target": "benchmarks/api/latency.py"},
    {"category": "support/benchmarks", "source": "scripts/benchmark_commerce_clustering.py (removed 1.58; moved to benchmarks/backend/commerce_clustering.py)", "target": "benchmarks/backend/commerce_clustering.py"},
    {"category": "support/benchmarks", "source": "scripts/benchmark_stage2.py (removed 1.58; moved to benchmarks/backend/stage2_pipeline.py)", "target": "benchmarks/backend/stage2_pipeline.py"},
    {"category": "support/infra", "source": "scripts/stage_smoke.ps1 (removed 1.58; moved to infra/scripts/staging_smoke.ps1)", "target": "infra/scripts/staging_smoke.ps1"},
    {"category": "support/infra", "source": "scripts/teardown_verify.ps1 (removed 1.58; moved to infra/scripts/teardown_verify.ps1)", "target": "infra/scripts/teardown_verify.ps1"},
    {"category": "support/infra", "source": "scripts/swap_task_image.py (removed 1.58; moved to infra/scripts/swap_task_image.py)", "target": "infra/scripts/swap_task_image.py"},
    {"category": "support/llm", "source": "scripts/gen_vllm_modelinfo_cache.py (removed 1.58; moved to services/product/llm_service/scripts/)", "target": "services/product/llm_service/scripts/"},
    {"category": "support/shared", "source": "scripts/upload_weights_s3.py (removed 1.58; moved to scripts/model_assets/upload.py)", "target": "scripts/model_assets/upload.py"},
    {"category": "support/shared", "source": "services/scripts/fetch_weights.sh (removed 1.58; moved to scripts/model_assets/fetch_weights.sh)", "target": "scripts/model_assets/fetch_weights.sh"},
    {"category": "cross-cutting", "source": ".env.example", "target": ".env.example"},
    {"category": "cross-cutting", "source": "pyproject.toml", "target": "pyproject.toml (tool-only) and services/product/*_service/pyproject.toml"},
    {"category": "cross-cutting", "source": "uv.lock", "target": "uv.lock (tool-only) and services/product/*_service/uv.lock"},
    {"category": "cross-cutting", "source": ".dockerignore", "target": ".dockerignore (retained fallback) and service Dockerfile.dockerignore files"},
    {"category": "cross-cutting", "source": "services/.dockerignore", "target": ".dockerignore (retained fallback) and service Dockerfile.dockerignore files"},
    {"category": "cross-cutting", "source": ".github/workflows/", "target": ".github/workflows/ (retained; update build and validation paths)"},
    {"category": "cross-cutting", "source": "infra/modules/compute/", "target": "infra/modules/compute/ (retained; update service image, Cloud Map, and task references)"},
    {"category": "cross-cutting", "source": "infra/environments/", "target": "infra/environments/ (retained; update service image and adapter variables)"},
    {"category": "cross-cutting", "source": "docs/", "target": "docs/ (retained; update commands, diagrams, and runbooks)"}
  ]
}
```
<!-- service-ownership-map:end -->

## Cross-cutting references to update during moves

- Docker build contexts, `Dockerfile` paths, `entrypoint.sh` paths, root and service
  ignore files, package metadata/locks, ASGI commands, CI workflow matrices, and
  service image names.
- Python imports and test imports from `core` or `providers`; Workbench static
  URLs; provider examples; benchmark and operational script paths.
- Terraform compute task definitions, Cloud Map discovery, image variables, and
  database/runtime references. Terraform remains under `infra/`; it is never
  moved into a platform runtime directory.
- Documentation, Compose references, environment examples, and smoke commands.

## Intentionally retained paths

- `infra/` stays the cloud-provisioning owner. `infra/modules/database/` is the
  source of managed RDS and ElastiCache configuration; platform directories later
  contain only local/runtime configuration and smoke assets.
- Root `pyproject.toml` and `uv.lock` remain for repository tools after product
  services receive independent package metadata and locks.
- Root `.dockerignore`, `.gitignore`, `.github/`, `docs/`, `openspec/`, and
  `plans/` remain repository-level control-plane assets.
- `archived/`, `notes/`, and `notebooks/` are outside this runtime migration and
  are not moved or deleted by it.
