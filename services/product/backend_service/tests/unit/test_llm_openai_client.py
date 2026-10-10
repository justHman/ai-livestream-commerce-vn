"""Offline tests for the canonical OpenAI-compatible LLM client.

The migrated ``test_llm_remote_engine.py`` (core/tests/test_llm_remote_client.py)
tested the core ``openai_compat`` engine adapter. In the service split the
llm_service deliberately REJECTS hosted adapters (``openai_compat`` is not in
its ENGINES registry — see llm_service/tests/unit/test_engine_selection.py);
the remote OpenAI-compatible transport is owned by the backend control plane
as ``backend.application.clients.llm.OpenAICompatibleClient``. These tests
cover that canonical client with httpx MockTransport — no real network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.application.clients.llm import (
    ChatMessage,
    ChatRequest,
    LLMClientError,
    OpenAICompatibleClient,
)


def _transport(*, stream_deltas: list[str] | None = None) -> httpx.MockTransport:
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


def test_requires_base_url(monkeypatch):
    """No base_url and no env LLM_BASE_URL -> typed error naming base_url."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    with pytest.raises(LLMClientError, match="base_url"):
        OpenAICompatibleClient(base_url="")


def test_chat_returns_text():
    client = httpx.Client(transport=_transport(stream_deltas=["hello world"]))
    engine = OpenAICompatibleClient(
        base_url="http://llm:8001", model="test-model", http_client=client
    )

    result = engine.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert result.text == "hello world"
    assert result.finish_reason == "stop"
    assert result.engine == "openai_compatible"
    assert result.num_prompt_tokens == 3
    client.close()


def test_chat_stream_yields_deltas():
    client = httpx.Client(transport=_transport(stream_deltas=["Xin ", "chào"]))
    engine = OpenAICompatibleClient(base_url="http://llm:8001/", model="m", http_client=client)

    deltas = list(engine.chat_stream(ChatRequest(messages=[ChatMessage(role="user", content="g")])))

    assert deltas == ["Xin ", "chào"]
    client.close()


def test_http_error_surfaces_clear_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = OpenAICompatibleClient(base_url="http://llm:8001", model="m", http_client=client)
    with pytest.raises(LLMClientError, match="HTTP 503"):
        engine.chat(ChatRequest(messages=[ChatMessage(role="user", content="x")]))
    client.close()


def test_stream_http_error_surfaces_clear_message():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = OpenAICompatibleClient(base_url="http://llm:8001", model="m", http_client=client)
    with pytest.raises(LLMClientError, match="HTTP 500"):
        list(engine.chat_stream(ChatRequest(messages=[ChatMessage(role="user", content="x")])))
    client.close()


def test_retry_after_request_error():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("first attempt refused")
        return httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
                ],
                "usage": {},
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    engine = OpenAICompatibleClient(
        base_url="http://llm:8001", model="m", max_retries=1, http_client=client
    )

    result = engine.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))

    assert calls["n"] == 2
    assert result.text == "ok"
    client.close()


def test_chat_path_is_configurable_for_other_providers(monkeypatch):
    monkeypatch.setenv("LLM_CHAT_PATH", "/chat/completions")
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"choices": [{"message": {"content": "xin chào"}}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    llm = OpenAICompatibleClient("https://host/v1beta/openai/", model="m", http_client=client)
    llm.chat(ChatRequest(messages=[ChatMessage(role="user", content="hi")]))
    assert seen == ["/v1beta/openai/chat/completions"]
    client.close()


def test_reasoning_effort_is_sent_only_when_configured(monkeypatch):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    request = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    OpenAICompatibleClient("https://h", model="m", http_client=client).chat(request)
    monkeypatch.setenv("LLM_REASONING_EFFORT", "low")
    OpenAICompatibleClient("https://h", model="m", http_client=client).chat(request)
    client.close()
    assert "reasoning_effort" not in bodies[0]
    assert bodies[1]["reasoning_effort"] == "low"


def test_omit_params_drops_fields_a_provider_rejects(monkeypatch):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    request = ChatRequest(messages=[ChatMessage(role="user", content="hi")])
    client = httpx.Client(transport=httpx.MockTransport(handler))
    OpenAICompatibleClient("https://h", model="m", http_client=client).chat(request)
    monkeypatch.setenv("LLM_OMIT_PARAMS", "seed, top_p")
    OpenAICompatibleClient("https://h", model="m", http_client=client).chat(request)
    client.close()
    assert "seed" in bodies[0] and "top_p" in bodies[0]
    assert "seed" not in bodies[1] and "top_p" not in bodies[1] and "model" in bodies[1]
