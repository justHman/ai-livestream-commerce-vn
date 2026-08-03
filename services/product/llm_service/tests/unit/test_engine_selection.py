"""Engine selection: registry load and legacy rejection."""

from __future__ import annotations

import pytest

from llm.engines.base import ENGINES, load_engine


def test_self_host_engines_registered() -> None:
    assert {"vllm", "sglang", "transformers"} <= set(ENGINES)


def test_llamacpp_not_registered() -> None:
    assert "llamacpp" not in ENGINES


def test_openai_compat_not_registered() -> None:
    assert "openai_compat" not in ENGINES


def test_load_noop_when_none() -> None:
    e = load_engine({"engine": "none"})
    assert e.generate.__class__.__name__  # noqa — noop has generate
    from llm.engines.base import LLMRequest

    resp = e.generate(LLMRequest(messages=[{"role": "user", "content": "hello"}]))
    assert resp.text.startswith("[noop]")
    assert resp.engine == "none"


def test_load_unknown_rejected() -> None:
    with pytest.raises(KeyError):
        load_engine({"engine": "remote_http"})