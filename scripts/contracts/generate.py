"""Generate deterministic v1 contract artifacts for every product service.

Usage:
    python scripts/contracts/generate.py [--scope backend|llm|tts|avatar]

Artifacts are service-owned (no root ``contracts/`` registry):
    backend  -> services/product/backend_service/contracts/v1/
        openapi.json
        websocket/control.schema.json
        websocket/platform.schema.json
    llm      -> services/product/llm_service/contracts/v1/openapi.json
    tts      -> services/product/tts_service/contracts/v1/openapi.json
    avatar   -> services/product/avatar_service/contracts/v1/openapi.json

Determinism: JSON serialized with sort_keys + indent=2 and a trailing
newline; no server URLs, no timestamps. Operational health routes
(``/health/live``, ``/health/ready``, and their unversioned/versioned
aliases) never appear in a v1 artifact. WebSocket schemas are stable
hand-authored contracts (FastAPI does not emit WebSocket routes into
OpenAPI documents), modeled on the backend ``application`` event models
and the workbench WS contract verified MATCH in cluster 1.42-1.49.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE_SRCS = (
    ROOT / "services/product/backend_service/src",
    ROOT / "services/product/llm_service/src",
    ROOT / "services/product/tts_service/src",
    ROOT / "services/product/avatar_service/src",
)

SERVICES = {
    "backend": {
        "package": "backend",
        "src": SERVICE_SRCS[0],
        "out_dir": ROOT / "services/product/backend_service/contracts/v1",
        "keep_prefix": "/api/v1",
        "ws_schemas": True,
    },
    "llm": {
        "package": "llm",
        "src": SERVICE_SRCS[1],
        "out_dir": ROOT / "services/product/llm_service/contracts/v1",
        "keep_prefix": "/v1",
        "ws_schemas": False,
    },
    "tts": {
        "package": "tts",
        "src": SERVICE_SRCS[2],
        "out_dir": ROOT / "services/product/tts_service/contracts/v1",
        "keep_prefix": "/v1",
        "ws_schemas": False,
    },
    "avatar": {
        "package": "avatar",
        "src": SERVICE_SRCS[3],
        "out_dir": ROOT / "services/product/avatar_service/contracts/v1",
        "keep_prefix": "/v1",
        "ws_schemas": False,
    },
}

# Versioned health aliases still mounted in the canonical app (legacy); the
# production contract excludes operational health entirely (OpenSpec 1.20).
_HEALTH_PATHS = {
    "/api/v1/health",
    "/api/v1/health/live",
    "/api/v1/health/ready",
    "/health",
    "/health/live",
    "/health/ready",
}


def _is_health_path(path: str) -> bool:
    return path in _HEALTH_PATHS or path.startswith("/health/")


def load_app(service: dict):
    """Import the canonical ``<package>.main:app`` with sibling srcs on path."""
    for src in SERVICE_SRCS:
        sys.path.insert(0, str(src))
    module = importlib.import_module(f"{service['package']}.main")
    return module.app


def openapi_spec(app) -> dict:
    """Deterministic OpenAPI for one service app, v1 paths only, no health."""
    spec = app.openapi()
    spec["paths"] = {
        path: operations
        for path, operations in spec.get("paths", {}).items()
        if path.startswith("/api/v1" if path.startswith("/api/") else "/v1/")
        and not _is_health_path(path)
    }
    # A v1 artifact never carries server URLs or timestamps.
    spec.pop("servers", None)
    return spec


def dump_json(data: dict) -> bytes:
    """Canonical serialization: sort keys, 2-space indent, trailing newline."""
    return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")


# ── WebSocket schemas (hand-authored; see module docstring) ──────────────

_CONTROL_CLIENT = {
    "oneOf": [
        {
            "type": "object",
            "properties": {"type": {"const": "interrupt"}},
            "required": ["type"],
            "additionalProperties": False,
        },
        {
            "type": "object",
            "properties": {"type": {"const": "ping"}},
            "required": ["type"],
            "additionalProperties": False,
        },
    ],
}

_CONTROL_EVENTS = {
    "control.connected": {
        "type": "object",
        "properties": {
            "type": {"const": "control.connected"},
            "session_id": {"type": "string"},
        },
        "required": ["type", "session_id"],
        "additionalProperties": False,
    },
    "pong": {
        "type": "object",
        "properties": {"type": {"const": "pong"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "error": {
        "type": "object",
        "properties": {
            "type": {"const": "error"},
            "detail": {"type": "string"},
        },
        "required": ["type", "detail"],
        "additionalProperties": False,
    },
    "avatar.speak_started": {
        "type": "object",
        "properties": {
            "type": {"const": "avatar.speak_started"},
            "text": {"type": "string"},
        },
        "required": ["type", "text"],
        "additionalProperties": False,
    },
    "avatar.speak_ended": {
        "type": "object",
        "properties": {
            "type": {"const": "avatar.speak_ended"},
            "reply": {"type": "string"},
        },
        "required": ["type", "reply"],
        "additionalProperties": False,
    },
    "avatar.video_window": {
        "type": "object",
        "properties": {
            "type": {"const": "avatar.video_window"},
            "seq": {"type": "integer"},
            "is_final": {"type": "boolean"},
            "duration_ms": {"type": "number"},
        },
        "required": ["type", "seq", "is_final", "duration_ms"],
        "additionalProperties": False,
    },
    "avatar.interrupted": {
        "type": "object",
        "properties": {"type": {"const": "avatar.interrupted"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "session.stopped": {
        "type": "object",
        "properties": {"type": {"const": "session.stopped"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "director.cycle_started": {
        "type": "object",
        "properties": {"type": {"const": "director.cycle_started"}},
        "required": ["type"],
        "additionalProperties": False,
    },
    "director.decision": {
        "type": "object",
        "properties": {
            "type": {"const": "director.decision"},
            "turn_id": {"type": "string"},
            "action": {"type": "string"},
            "product": {"type": ["string", "null"]},
            "field": {"type": ["string", "null"]},
            "stage": {"type": "string"},
            "task_id": {"type": "string"},
            "may_interrupt": {"type": "boolean"},
            "reason": {"type": "string"},
        },
        "required": [
            "type",
            "turn_id",
            "action",
            "product",
            "field",
            "stage",
            "task_id",
            "may_interrupt",
            "reason",
        ],
        "additionalProperties": False,
    },
    "director.spoke": {
        "type": "object",
        "properties": {
            "type": {"const": "director.spoke"},
            "snapshot_at": {"type": "number"},
            "profile_revision": {"type": "integer"},
            "catalog_revision": {"type": "integer"},
            "config_revision": {"type": "integer"},
            "generation_token": {"type": "string"},
            "accepted_snapshot": {"type": "object"},
            "pivot_state": {"type": "object"},
            "answer_cache": {"type": "object"},
            "received_total": {"type": "integer"},
            "buffered_comments": {"type": "integer"},
            "active_comments": {"type": "integer"},
            "director_cycles": {"type": "integer"},
            "active_decision": {"type": ["object", "null"]},
            "queued_decisions": {"type": "integer"},
            "queued_decisions_detail": {"type": "array"},
            "completed_speeches": {"type": "integer"},
            "completed_speech_history": {"type": "array"},
            "queue": {"type": "object"},
            "speech_queue": {"type": "object"},
            "last_decision_ms_ago": {"type": ["number", "null"]},
            "skips": {"type": "integer"},
            "interrupts": {"type": "integer"},
            "decisions_emitted": {"type": "integer"},
        },
        "required": ["type"],
        "additionalProperties": True,
    },
    "coordinator.speak_started": {
        "type": "object",
        "properties": {
            "type": {"const": "coordinator.speak_started"},
            "turn_id": {"type": "string"},
            "state": {"const": "processing"},
            "stage": {"type": "string"},
            "task_id": {"type": "string"},
            "action": {"type": "string"},
            "product": {"type": ["string", "null"]},
        },
        "required": ["type", "turn_id", "state", "stage", "task_id", "action"],
        "additionalProperties": False,
    },
    "coordinator.retry_scheduled": {
        "type": "object",
        "properties": {
            "type": {"const": "coordinator.retry_scheduled"},
            "turn_id": {"type": "string"},
            "retry_count": {"type": "integer"},
            "error": {"type": "string"},
        },
        "required": ["type", "turn_id", "retry_count", "error"],
        "additionalProperties": False,
    },
    "coordinator.speak_finished": {
        "type": "object",
        "properties": {
            "type": {"const": "coordinator.speak_finished"},
            "turn_id": {"type": "string"},
            "state": {"const": "completed"},
            "stage": {"type": "string"},
            "task_id": {"type": "string"},
            "action": {"type": "string"},
            "product_id": {"type": ["string", "null"]},
        },
        "required": ["type", "turn_id", "state", "stage", "task_id", "action"],
        "additionalProperties": False,
    },
    "coordinator.speak_failed": {
        "type": "object",
        "properties": {
            "type": {"const": "coordinator.speak_failed"},
            "turn_id": {"type": "string"},
            "state": {"type": "string"},
            "action": {"type": "string"},
            "product_id": {"type": ["string", "null"]},
            "stage": {"type": "string"},
            "task_id": {"type": "string"},
            "error": {"type": "string"},
            "attempt": {"type": "integer"},
        },
        "required": [
            "type",
            "turn_id",
            "state",
            "action",
            "stage",
            "task_id",
            "error",
        ],
        "additionalProperties": False,
    },
    "coordinator.terminal_failure": {
        "type": "object",
        "properties": {
            "type": {"const": "coordinator.terminal_failure"},
            "state": {"type": "string"},
        },
        "required": ["type", "state"],
        "additionalProperties": False,
    },
    "engine.llm_swap_started": {
        "type": "object",
        "properties": {
            "type": {"const": "engine.llm_swap_started"},
            "engine": {"type": "string"},
            "model": {"type": "string"},
        },
        "required": ["type", "engine", "model"],
        "additionalProperties": False,
    },
    "engine.llm_swap_failed": {
        "type": "object",
        "properties": {
            "type": {"const": "engine.llm_swap_failed"},
            "error": {"type": "string"},
        },
        "required": ["type", "error"],
        "additionalProperties": False,
    },
    "engine.llm_swapped": {
        "type": "object",
        "properties": {
            "type": {"const": "engine.llm_swapped"},
            "engine": {"type": "string"},
            "model": {"type": "string"},
        },
        "required": ["type", "engine", "model"],
        "additionalProperties": False,
    },
    "engine.tts_swap_started": {
        "type": "object",
        "properties": {
            "type": {"const": "engine.tts_swap_started"},
            "engine": {"type": "string"},
            "model": {"type": "string"},
        },
        "required": ["type", "engine", "model"],
        "additionalProperties": False,
    },
    "engine.tts_swap_failed": {
        "type": "object",
        "properties": {
            "type": {"const": "engine.tts_swap_failed"},
            "error": {"type": "string"},
        },
        "required": ["type", "error"],
        "additionalProperties": False,
    },
    "engine.tts_swapped": {
        "type": "object",
        "properties": {
            "type": {"const": "engine.tts_swapped"},
            "engine": {"type": "string"},
            "model": {"type": "string"},
            "sample_rate": {"type": "integer"},
        },
        "required": ["type", "engine", "model", "sample_rate"],
        "additionalProperties": False,
    },
}

CONTROL_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "backend/contracts/v1/websocket/control.schema.json",
    "title": "Backend control WebSocket channel",
    "description": (
        "Server-pushed control events and client commands on "
        "/api/v1/ws/control/{session_id}. Events are emitted by the backend "
        "application layer (session lifecycle, avatar speak, Director "
        "decisions, coordinator speech plan, engine swaps) and consumed by "
        "the workbench control client."
    ),
    "type": "object",
    "definitions": {
        "clientMessage": _CONTROL_CLIENT,
        "serverEvent": {
            "type": "object",
            "oneOf": [{"$ref": f"#/definitions/events/{name}"} for name in sorted(_CONTROL_EVENTS)],
            "required": ["type"],
        },
        "events": {name: _CONTROL_EVENTS[name] for name in sorted(_CONTROL_EVENTS)},
    },
    "properties": {
        "clientMessage": {"$ref": "#/definitions/clientMessage"},
        "serverEvent": {"$ref": "#/definitions/serverEvent"},
    },
}

_PLATFORM_CLIENT = {
    "type": "object",
    "properties": {
        "text": {"type": "string", "minLength": 1, "maxLength": 500},
        "author": {"type": "string", "maxLength": 128},
        "ts": {"type": "number"},
    },
    "required": ["text"],
    "additionalProperties": False,
}

_PLATFORM_EVENTS = {
    "platform.connected": {
        "type": "object",
        "properties": {
            "type": {"const": "platform.connected"},
            "session_id": {"type": "string"},
        },
        "required": ["type", "session_id"],
        "additionalProperties": False,
    },
    "platform.accepted": {
        "type": "object",
        "properties": {
            "type": {"const": "platform.accepted"},
            "comment_id": {"type": "string"},
        },
        "required": ["type", "comment_id"],
        "additionalProperties": False,
    },
    "platform.stored": {
        "type": "object",
        "properties": {
            "type": {"const": "platform.stored"},
            "pending": {"type": "integer"},
        },
        "required": ["type", "pending"],
        "additionalProperties": False,
    },
    "error": {
        "type": "object",
        "properties": {
            "type": {"const": "error"},
            "detail": {"type": "string"},
        },
        "required": ["type", "detail"],
        "additionalProperties": False,
    },
}

PLATFORM_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "$id": "backend/contracts/v1/websocket/platform.schema.json",
    "title": "Backend platform WebSocket channel",
    "description": (
        "Viewer chat ingress on /api/v1/ws/platform/{session_id}. The "
        "client posts {text, author?, ts?}; the backend replies with an "
        "acceptance/storage event or an error. Consumed by the workbench "
        "platform client."
    ),
    "type": "object",
    "definitions": {
        "clientMessage": _PLATFORM_CLIENT,
        "serverEvent": {
            "type": "object",
            "oneOf": [
                {"$ref": f"#/definitions/events/{name}"} for name in sorted(_PLATFORM_EVENTS)
            ],
            "required": ["type"],
        },
        "events": {name: _PLATFORM_EVENTS[name] for name in sorted(_PLATFORM_EVENTS)},
    },
    "properties": {
        "clientMessage": {"$ref": "#/definitions/clientMessage"},
        "serverEvent": {"$ref": "#/definitions/serverEvent"},
    },
}


def generate_artifacts(service_name: str) -> list[Path]:
    """Regenerate the committed artifacts for one service; return written paths."""
    service = SERVICES[service_name]
    app = load_app(service)
    out_dir = service["out_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    written = [out_dir / "openapi.json"]
    written[0].write_bytes(dump_json(openapi_spec(app)))
    if service["ws_schemas"]:
        ws_dir = out_dir / "websocket"
        ws_dir.mkdir(parents=True, exist_ok=True)
        control = ws_dir / "control.schema.json"
        platform = ws_dir / "platform.schema.json"
        control.write_bytes(dump_json(CONTROL_SCHEMA))
        platform.write_bytes(dump_json(PLATFORM_SCHEMA))
        written += [control, platform]
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--scope",
        choices=sorted(SERVICES),
        default=None,
        help="Regenerate only this service's artifacts (default: all).",
    )
    args = parser.parse_args()
    scopes = [args.scope] if args.scope else sorted(SERVICES)
    for name in scopes:
        for target in generate_artifacts(name):
            print(f"wrote {target.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
