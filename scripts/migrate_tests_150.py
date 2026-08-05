#!/usr/bin/env python3
"""One-shot migration helper for OpenSpec 1.50: copy core/tests/*.py to
owner locations with canonical import rewrites.

Handles the mapping table below; runs standalone with stdlib only.
"""
from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# core module -> canonical module (backend/llm/tts/avatar)
IMPORT_MAP = {
    "core.api.v1.hub": "backend.api.v1.hub",
    "core.api.v1": "backend.api.v1",
    "core.api.limits": "backend.application.render.limiters",
    "core.config": "backend.config",
    "core.engine_manager": "backend.engine_manager",
    "core.store": "backend.application.db",
    "core.db.postgres_store": "backend.application.db.postgres_store",
    "core.render.mock": "backend.application.render.mock",
    "core.render.base": "backend.application.render.engines_base",
    "core.render.windows": "backend.application.render.windows",
    "core.render.locks": "backend.application.render.locks",
    "core.render.queue": "backend.application.render.queue",
    "core.render.orchestrator": "backend.application.render.orchestrator",
    "core.render.limiters": "backend.application.render.limiters",
    "core.render.cloud": "backend.application.render.engines_base",
    "core.render.remote_avatar": "backend.application.clients.avatar.self_hosted",
    "core.render.self_host": "backend.application.render.engines_base",
    "core.llm.base": "llm.engines.base",
    "core.llm": "llm.engines.base",
    "core.llm.adapters.llamacpp": "llm.engines.llamacpp",
    "core.llm.adapters": "llm.engines",
    "core.tts.base": "tts.engines.base",
    "core.tts": "tts.engines.base",
    "core.tts.adapters": "tts.engines",
    "core.director": "backend.application.director",
    "core.director.catalog": "backend.application.director.catalog",
    "core.director.cluster": "backend.application.director.clustering",
    "core.director.config": "backend.application.director.config",
    "core.director.coordinator": "backend.application.director.coordinator",
    "core.director.director": "backend.application.director.decision",
    "core.director.embedder": "backend.application.director.embeddings",
    "core.director.hooks": "backend.application.director.hooks",
    "core.director.runtime": "backend.application.director.session_context",
    "core.director.scorer": "backend.application.director.scoring",
    "core.director.coverage": "backend.application.director.scoring",
    "core.director.routing": "backend.application.director.routing",
    "core.director.state": "backend.application.director.state",
    "core.director.chat_queue": "backend.application.director.comment_buffer",
    "core.director.pivot": "backend.application.director.pivot",
    "core.director.decision_preparation": "backend.application.director.decision_preparation",
    "core.schemas.run_plan": "backend.application.schemas.run_plan",
    "core.schemas.utterance": "backend.application.schemas.utterance",
    "core.stream.chunker": "backend.application.text_chunker",
    "core.livekit_publish": "backend.application.publishing",
    "core.livekit_tokens": "backend.application.publishing.livekit",
    "core.debug.mock_data": "backend.debug.mock_data",
}

# long dotted strings inside code (monkeypatch targets, __import__ strings)
DOTTED_MAP = {
    "core.engine_manager.load_llm_engine": "backend.engine_manager.load_llm_engine",
    "core.engine_manager.load_tts_engine": "backend.engine_manager.load_tts_engine",
    "core.api.v1": "backend.api.v1",
    "core.director.config": "backend.application.director.config",
    "core.director.state": "backend.application.director.state",
}


def rewrite(source: str) -> str:
    # 1. Longest-first import rewrites for `from core.X import Y` / `import core.X`
    lines = source.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        rewritten = line
        for old, new in sorted(IMPORT_MAP.items(), key=lambda kv: -len(kv[0])):
            if re.search(rf"^(from|import) {re.escape(old)}( |$|\()", rewritten):
                rewritten = re.sub(
                    rf"^(from|import) {re.escape(old)}( |$|\()",
                    rf"\1 {new}\2",
                    rewritten,
                )
        # dotted string literals (monkeypatch.setattr("core....", ...))
        for old, new in DOTTED_MAP.items():
            rewritten = rewritten.replace(f'"{old}"', f'"{new}"')
            rewritten = rewritten.replace(f"'{old}'", f"'{new}'")
        out.append(rewritten)
    return "".join(out)


def main() -> int:
    plan = [
        # (src core file, dst owner file relative to services/product/<svc>_service/)
        ("test_sessions_api.py", "backend_service/tests/integration/test_session_routes.py"),
        ("test_avatars_api.py", "backend_service/tests/integration/test_avatar_routes.py"),
        ("test_engines_endpoint.py", "backend_service/tests/integration/test_voice_routes.py"),
        ("test_api_auth.py", "backend_service/tests/integration/test_api_security.py"),
        ("test_ws_auth.py", "backend_service/tests/integration/test_control_websocket.py"),
        ("test_api_limits.py", "backend_service/tests/integration/test_api_limits.py"),
        ("test_api_persist.py", "backend_service/tests/integration/test_api_persist.py"),
        ("test_platform_ws.py", "backend_service/tests/integration/test_platform_websocket.py"),
        ("test_backend_api_security.py", "backend_service/tests/integration/test_api_security_helpers.py"),
        ("test_bootstrap_container.py", "backend_service/tests/integration/test_app_factory_container.py"),
        ("test_embedder_readiness.py", "backend_service/tests/integration/test_embedder_readiness.py"),
        ("test_runtime_discovery_preview.py", "backend_service/tests/integration/test_voice_routes_discovery.py"),
        ("test_session_concurrency.py", "backend_service/tests/integration/test_session_concurrency.py"),
        ("test_lite_chat_integration.py", "backend_service/tests/integration/test_lite_chat_integration.py"),
        ("test_editable_session_config.py", "backend_service/tests/integration/test_session_config.py"),
        ("test_mjpeg_continuous.py", "backend_service/tests/integration/test_mock_media_absent.py"),
        ("test_sandbox_verification.py", "backend_service/tests/integration/test_sandbox_route_absent.py"),
        ("test_director_timers.py", "backend_service/tests/unit/test_director_timers.py"),
        ("test_director_coordinator.py", "backend_service/tests/unit/test_director_coordinator.py"),
        ("test_coordinator_multisession.py", "backend_service/tests/unit/test_coordinator_multisession.py"),
        ("test_queue_coordinator.py", "backend_service/tests/unit/test_playback_queue.py"),
        ("test_queue_metrics.py", "backend_service/tests/unit/test_queue_metrics.py"),
        ("test_chat_queue.py", "backend_service/tests/unit/test_comment_buffer.py"),
        ("test_commerce_clustering.py", "backend_service/tests/unit/test_comment_clustering.py"),
        ("test_run_plan.py", "backend_service/tests/unit/test_run_plan.py"),
        ("test_text_chunker.py", "backend_service/tests/unit/test_text_chunker.py"),
        ("test_stage2_auto_demo_sequence.py", "backend_service/tests/unit/test_director_decisions.py"),
        ("test_stage2_diagnostics.py", "backend_service/tests/unit/test_decision_preparation.py"),
        ("test_benchmark_stage2.py", "backend_service/tests/unit/test_benchmark_contracts.py"),
        ("test_livekit_publish_registry.py", "backend_service/tests/integration/test_livekit_publishing.py"),
        ("test_livekit_token.py", "backend_service/tests/integration/test_livekit_token.py"),
        ("test_postgres_schema.py", "backend_service/tests/integration/test_postgres_runtime_store.py"),
        ("test_postgres_store_lifecycle.py", "backend_service/tests/integration/test_postgres_store_lifecycle.py"),
        ("test_server_pg_lifecycle.py", "backend_service/tests/integration/test_server_pg_lifecycle.py"),
        ("test_llm_streaming.py", "llm_service/tests/unit/test_streaming.py"),
        ("test_llm_remote_client.py", "llm_service/tests/unit/test_openai_compat_engine.py"),
        ("test_tts_streaming.py", "tts_service/tests/unit/test_streaming.py"),
        ("test_tts_remote_client.py", "tts_service/tests/unit/test_remote_engine.py"),
        ("test_tts_presets.py", "tts_service/tests/unit/test_presets.py"),
        ("test_audio_windowing.py", "avatar_service/tests/unit/test_windows.py"),
        ("test_mock_render_lifecycle.py", "avatar_service/tests/unit/test_mock_lifecycle.py"),
        ("test_mock_frame_generation.py", "avatar_service/tests/unit/test_mock_frames.py"),
        ("test_idle_loop.py", "avatar_service/tests/unit/test_idle_loop.py"),
        ("test_render_backend_enum.py", "avatar_service/tests/unit/test_engine_selection_backend.py"),
        ("test_render_stop_all.py", "avatar_service/tests/unit/test_render_stop_all.py"),
        ("test_remote_avatar.py", "avatar_service/tests/unit/test_self_hosted_client.py"),
        ("test_pipecat_bridge.py", "backend_service/tests/unit/test_pipecat_config.py"),
        ("test_livekit_publish_sdk.py", "avatar_service/tests/unit/test_livekit_publish_sdk.py"),
        ("test_livekit_publish_stub.py", "avatar_service/tests/unit/test_livekit_publish_stub.py"),
        ("test_liveavatar_playback_timeout.py", "backend_service/tests/unit/test_liveavatar_playback.py"),
    ]
    tests_dir = ROOT / "core" / "tests"
    svc = ROOT / "services" / "product"
    for src_name, dst_rel in plan:
        src = tests_dir / src_name
        dst = svc / dst_rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8")
        rewritten = rewrite(text)
        dst.write_text(rewritten, encoding="utf-8")
        print(f"migrated {src_name} -> {dst_rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
