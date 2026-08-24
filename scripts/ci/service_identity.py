"""Canonical service identity map (audit R1.8).

Single source of truth for service_id in {backend, llm, tts, avatar} across
build -> deploy -> evidence -> migration. Never derive service identity from
human/resource display strings; every workflow matrix row must match this
table (enforced by tests/ci/test_service_identity.py).
"""

import json
from pathlib import Path
from typing import Dict, FrozenSet

_IDENTITY_PATH = Path(__file__).resolve().parent / "service_identity.json"

_identity: Dict[str, Dict[str, str]] = {}


def _load() -> Dict[str, Dict[str, str]]:
    global _identity
    if not _identity:
        with open(_IDENTITY_PATH, "r", encoding="utf-8") as f:
            _identity = json.load(f)
    return _identity


def service_ids() -> FrozenSet[str]:
    """Canonical service_id set: backend, llm, tts, avatar."""
    return frozenset(_load().keys())


def identity(service_id: str) -> Dict[str, str]:
    """Row for a canonical service_id."""
    return _load()[service_id]


def by_service_dir() -> Dict[str, str]:
    """service_dir (e.g. backend_service) -> canonical service_id."""
    return {v["service_dir"]: k for k, v in _load().items()}


def image(service_id: str) -> str:
    return _load()[service_id]["image"]


def container(service_id: str) -> str:
    return _load()[service_id]["container"]
