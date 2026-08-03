"""OpenAI-compatible LLM outbound client (backend-owned).

Canonical outbound transport (Task 1.22/1.32): calls a self-host or hosted
OpenAI-compatible chat completions endpoint and returns typed results. Owns
only serialization, server-side authentication, network I/O, bounded timeout/
retry, response parsing, and typed transport errors. No model-engine code.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Iterator, Optional

import httpx


class LLMClientError(RuntimeError):
    """Typed transport failure for an LLM client."""


@dataclass
class LLMResult:
    """Parsed chat completion result."""

    text: str
    finish_reason: str = "stop"
    num_prompt_tokens: int = 0
    num_generated_tokens: int = 0
    engine: str = ""


@dataclass
class ChatMessage:
    """One message in the chat conversation."""

    role: str
    content: str


@dataclass
class ChatRequest:
    """Parameters for a chat completion call."""

    messages: list[ChatMessage]
    model: str = ""
    max_tokens: int = 512
    temperature: float = 0.7
    top_p: float = 0.95
    stop: list[str] = field(default_factory=list)
    seed: int = 42
    stream: bool = False


class OpenAICompatibleClient:
    """HTTP client for OpenAI-compatible chat completions endpoints.

    Calls the configured base URL (self-host LLM service or OpenAI) with
    server-side credentials. No engine code, no API/Director imports.
    """

    def __init__(
        self,
        base_url: str = "",
        *,
        api_key: str = "",
        model: str = "",
        timeout: float = 60.0,
        max_retries: int = 0,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        base = (base_url or os.environ.get("LLM_BASE_URL", "") or "").strip()
        if not base:
            raise LLMClientError("OpenAICompatibleClient needs base_url or env LLM_BASE_URL")
        self._base_url = base.rstrip("/")
        self._api_key = api_key or os.environ.get("LLM_AUTH_TOKEN", "") or ""
        self._model = model or os.environ.get("LLM_MODEL", "") or ""
        self._timeout = float(timeout)
        self._max_retries = int(max_retries)
        self._client = http_client

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def _build_body(self, req: ChatRequest) -> dict:
        msgs = [{"role": m.role, "content": m.content} for m in req.messages]
        body: dict = {
            "model": req.model or self._model,
            "messages": msgs,
            "max_tokens": req.max_tokens,
            "temperature": req.temperature,
            "top_p": req.top_p,
            "stream": req.stream,
        }
        if req.stop:
            body["stop"] = req.stop
        if req.seed:
            body["seed"] = req.seed
        return body

    def chat(self, req: ChatRequest) -> LLMResult:
        """Blocking chat completion. Returns the full response."""
        client = self._get_client()
        url = f"{self._base_url}/v1/chat/completions"
        body = self._build_body(req)
        last_error: Optional[Exception] = None
        for attempt in range(max(1, self._max_retries + 1)):
            try:
                resp = client.post(url, json=body, headers=self._headers())
            except httpx.RequestError as exc:
                last_error = exc
                continue
            try:
                resp.raise_for_status()
            except httpx.HTTPStatusError as exc:
                detail = (resp.text or "")[:300]
                raise LLMClientError(f"LLM chat: HTTP {resp.status_code} {detail}") from exc
            data = resp.json() if resp.content else {}
            choices = data.get("choices", [])
            if not choices:
                return LLMResult(text="", engine="openai_compatible")
            choice = choices[0]
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            text = message.get("content", "") if isinstance(message, dict) else ""
            finish = choice.get("finish_reason", "stop") if isinstance(choice, dict) else "stop"
            usage = data.get("usage", {}) or {}
            return LLMResult(
                text=text,
                finish_reason=finish,
                num_prompt_tokens=int(usage.get("prompt_tokens", 0)),
                num_generated_tokens=int(usage.get("completion_tokens", 0)),
                engine="openai_compatible",
            )
        raise LLMClientError(f"LLM chat failed after retries: {last_error}") from last_error

    def chat_stream(self, req: ChatRequest) -> Iterator[str]:
        """Streaming chat completion. Yields text deltas."""
        client = self._get_client()
        url = f"{self._base_url}/v1/chat/completions"
        body = self._build_body(req)
        body["stream"] = True
        try:
            with client.stream("POST", url, json=body, headers=self._headers()) as resp:
                resp.raise_for_status()
                for line in resp.iter_lines():
                    if not line or line.startswith(":"):
                        continue
                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                            choices = chunk.get("choices", [])
                            if choices:
                                delta = choices[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                        except json.JSONDecodeError:
                            continue
        except httpx.RequestError as exc:
            raise LLMClientError(f"LLM chat stream failed: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            detail = (resp.text or "")[:300]
            raise LLMClientError(f"LLM chat stream: HTTP {resp.status_code} {detail}") from exc

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
