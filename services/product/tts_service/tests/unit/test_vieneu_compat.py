"""Startup compatibility: pinned VieNeu v3 Turbo surface (Change T task 1.7).

Skipped when the optional vieneu extra is not installed, so unit runs stay
hermetic; the integration/contract suites and the pinned lockfile cover the
real dependency.
"""

from __future__ import annotations

import inspect

import pytest

vieneu = pytest.importorskip("vieneu")

V3_TURBO_MODE = "v3turbo"
V3_TURBO_BACKBONE = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
# Surface the v3 Turbo class must expose for the pinned adapter path.
REQUIRED_METHODS = (
    "infer",
    "infer_batch",
    "encode_reference",
    "add_voice",
    "list_preset_voices",
    "close",
)


def _v3_turbo_class():
    """Instantiate the v3 Turbo factory result once; construction is heavy
    (model registry probing, ~9 s), so tests share one class object."""
    return vieneu.Vieneu(mode=V3_TURBO_MODE).__class__


V3_TURBO_CLASS = _v3_turbo_class()


def test_vieneu_factory_exported() -> None:
    assert callable(vieneu.Vieneu)
    assert "mode" in inspect.signature(vieneu.Vieneu).parameters


def test_v3turbo_class_surface() -> None:
    cls = V3_TURBO_CLASS
    for method in REQUIRED_METHODS:
        assert callable(getattr(cls, method, None)), f"V3Turbo missing {method}"


def test_v3turbo_default_backbone_matches_pinned_revision() -> None:
    params = inspect.signature(V3_TURBO_CLASS.__init__).parameters
    assert "backbone_repo" in params
    assert params["backbone_repo"].default == V3_TURBO_BACKBONE


def test_v3turbo_accepts_auto_backend_and_batch_config() -> None:
    params = inspect.signature(V3_TURBO_CLASS.__init__).parameters
    for name in ("backend", "device", "max_batch_size"):
        assert name in params, f"V3Turbo missing ctor param {name}"
