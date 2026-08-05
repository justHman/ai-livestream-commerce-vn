"""Offline tests for remote OpenAI-compat LLM client (Task 9).

Uses httpx MockTransport — no real network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from llm.engines.base import ENGINES, LLMRequest, load_engine
from llm.engines.base import TextChunk


def _make_transport(*, stream_deltas: list[str] | None = None) -> httpx.MockTransport:
    deltas = list(stream_deltas or ["Xin ", "chào"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v1/chat/completions")
        body = json.loads(request.content.decode("utf-8"))
        if body.get("stream"):
            lines = []
            for d in deltas:
                chunk = {"choices": [{"delta": {"content": d}, "finish_reason": None}]}
                lines.append(f"data: {json.dumps(chunk)}\n\n")
            lines.append("data: [DONE]\n\n")
            return httpx.Response(
                200,
                content="".join(lines).encode("utf-8"),
                headers={"content-type": "text/event-stream"},
            )
        text = "".join(deltas)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 3, "completion_tokens": len(deltas)},
            },
        )

    return httpx.MockTransport(handler)


def test_openai_compat_registered():
    # Import adapters package to register engines.
    import core.llm.adapters  # noqa: F401

    assert "openai_compat" in ENGINES
    assert "remote" in ENGINES
    assert ENGINES["openai_compat"] is ENGINES["remote"]


def test_from_config_requires_base_url(monkeypatch):
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="base_url"):
        load_engine({"engine": "openai_compat", "model": "m"})


def test_generate_returns_text():
    transport = _make_transport(stream_deltas=["hello world"])
    client = httpx.Client(transport=transport)
    engine = load_engine(
        {
            "engine": "openai_compat",
            "base_url": "http://llm:8001",
            "model": "test-model",
            "http_client": client,
        }
    )
    resp = engine.generate(LLMRequest.from_prompt("hi"))
    assert resp.text == "hello world"
    assert resp.finish_reason == "stop"
    assert resp.engine == "openai_compat"
    assert resp.num_prompt_tokens == 3
    client.close()


def test_stream_chunks_yields_final_textchunk():
    transport = _make_transport(stream_deltas=["Xin ", "chào"])
    client = httpx.Client(transport=transport)
    engine = load_engine(
        {
            "engine": "openai_compat",
            "base_url": "http://llm:8001/",
            "model": "m",
            "http_client": client,
        }
    )
    req = LLMRequest.from_prompt("greet")
    chunks = list(engine.stream_chunks(req, session_id="s1", utterance_id="u1"))
    assert len(chunks) == 2
    assert all(isinstance(c, TextChunk) for c in chunks)
    assert [c.text for c in chunks] == ["Xin ", "chào"]
    assert [c.seq for c in chunks] == [0, 1]
    assert chunks[-1].is_final is True
    assert chunks[0].is_final is False
    assert chunks[0].session_id == "s1"
    assert chunks[0].utterance_id == "u1"
    client.close()


def test_http_error_surfaces_clear_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = load_engine(
        {
            "engine": "remote",
            "base_url": "http://llm:8001",
            "model": "m",
            "http_client": client,
        }
    )
    with pytest.raises(RuntimeError, match="HTTP 503"):
        engine.generate(LLMRequest.from_prompt("x"))
    client.close()


def test_guided_json_includes_schema_in_body():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": '{"speech":"hi","action":"wave","is_final":true}',
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    schema = {
        "title": "Utterance",
        "type": "object",
        "properties": {"speech": {"type": "string"}},
    }
    engine = load_engine(
        {
            "engine": "openai_compat",
            "base_url": "http://llm:8001",
            "model": "m",
            "guided_json": True,
            "http_client": client,
        }
    )
    req = LLMRequest.from_prompt("hi")
    req.response_schema = schema
    engine.generate(req)
    body = captured["body"]
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == schema
    assert body["extra_body"]["guided_json"] == schema
    client.close()
