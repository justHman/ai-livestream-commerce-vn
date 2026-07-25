"""Remote OpenAI-compatible LLM client (HTTP/SSE).

Talks to any server that exposes OpenAI chat completions:
  POST {base_url}/v1/chat/completions

Production path: backend orchestrator -> LLM service (vLLM OpenAI server).
Dev/offline path stays on in-process engines (none/llamacpp/hf).

Usage:
    llm = load_engine({
        "engine": "openai_compat",  # alias: "remote"
        "base_url": "http://llm:8001",
        "model": "Qwen/Qwen3.5-4B",
        "api_key": "",              # optional Bearer token
    })
"""

from __future__ import annotations

import json
import os
from typing import Any, Iterator, Optional

import httpx

from ..base import LLMEngine, LLMRequest, LLMResponse, register_engine


def _strip_trailing_slash(url: str) -> str:
    return url.rstrip("/")


def _chat_url(base_url: str) -> str:
    return f"{_strip_trailing_slash(base_url)}/v1/chat/completions"


def _auth_headers(api_key: str) -> dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def _payload(
    req: LLMRequest,
    model: str,
    *,
    stream: bool,
    guided_json: bool = False,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": list(req.messages),
        "max_tokens": req.max_tokens,
        "temperature": req.temperature,
        "top_p": req.top_p,
        "stream": stream,
    }
    if req.stop:
        body["stop"] = req.stop
    if req.seed is not None:
        body["seed"] = req.seed
    if req.frequency_penalty:
        body["frequency_penalty"] = req.frequency_penalty
    if req.repetition_penalty and req.repetition_penalty != 1.0:
        body["repetition_penalty"] = req.repetition_penalty
    if req.top_k is not None and req.top_k > 0:
        body["top_k"] = req.top_k
    # Outlines / guided JSON: attach OpenAI response_format + vLLM extra_body.
    schema = getattr(req, "response_schema", None)
    if guided_json and schema:
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": schema.get("title") or "Utterance",
                "schema": schema,
            },
        }
        # vLLM Outlines backend also accepts guided_json in extra_body.
        body["extra_body"] = {"guided_json": schema}
    return body


def _raise_http(resp: httpx.Response, action: str) -> None:
    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = (resp.text or "")[:300]
        raise RuntimeError(
            f"openai_compat {action} failed: HTTP {resp.status_code} {detail}"
        ) from exc


def _iter_sse_data_lines(resp: httpx.Response) -> Iterator[str]:
    """Yield payload strings after 'data:' from an SSE response body."""
    for raw in resp.iter_lines():
        if not raw:
            continue
        line = raw.strip() if isinstance(raw, str) else raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            return
        yield data


@register_engine("openai_compat")
class OpenAICompatEngine(LLMEngine):
    """HTTP client for OpenAI-compatible chat completions (sync httpx)."""

    def __init__(self) -> None:
        self._base_url: str = ""
        self._model: str = ""
        self._api_key: str = ""
        self._timeout: float = 60.0
        self._guided_json: bool = False
        self._client: Optional[httpx.Client] = None

    @classmethod
    def from_config(cls, cfg: dict) -> "OpenAICompatEngine":
        e = cls()
        base = (
            cfg.get("base_url")
            or os.environ.get("LLM_BASE_URL", "")
            or ""
        )
        base = str(base).strip()
        if not base:
            raise ValueError(
                "openai_compat needs cfg['base_url'] or env LLM_BASE_URL"
            )
        e._base_url = _strip_trailing_slash(base)
        e._model = str(cfg.get("model") or cfg.get("weights_path") or "default")
        e._api_key = str(
            cfg.get("api_key") or os.environ.get("LLM_API_KEY", "") or ""
        )
        e._timeout = float(cfg.get("timeout", 60.0))
        e._guided_json = bool(
            cfg.get("guided_json")
            or os.environ.get("LLM_GUIDED_JSON", "").lower()
            in ("1", "true", "on", "yes")
        )
        # Allow injecting a prebuilt client (tests); otherwise lazy-create.
        client = cfg.get("http_client")
        if client is not None:
            e._client = client
        e.name = "openai_compat"
        return e

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def generate(self, req: LLMRequest) -> LLMResponse:
        client = self._get_client()
        url = _chat_url(self._base_url)
        try:
            resp = client.post(
                url,
                json=_payload(
                    req, self._model, stream=False, guided_json=self._guided_json
                ),
                headers=_auth_headers(self._api_key),
            )
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"openai_compat generate request failed: {exc}"
            ) from exc
        _raise_http(resp, "generate")
        try:
            data = resp.json()
        except ValueError:
            # Some free endpoints (e.g. DeepSeek flash-free) return a JSON body
            # followed by SSE trailers ("data: [DONE]" / "HTTP ... time=..." from
            # curl proxies) even with stream=false. Strip everything after the
            # first complete JSON object so resp.json() can parse it.
            raw = resp.text or ""
            # Find the end of the first JSON object (matching braces).
            depth, end = 0, 0
            for i, ch in enumerate(raw):
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            if end > 0:
                import json as _json
                try:
                    data = _json.loads(raw[:end])
                except ValueError as exc:
                    raise RuntimeError(
                        "openai_compat generate returned non-JSON body"
                    ) from exc
            else:
                raise RuntimeError(
                    "openai_compat generate returned non-JSON body"
                )
        # Debug: log raw response shape to diagnose empty reply.
        import os as _os
        if _os.environ.get("DEBUG_ENABLED") == "1":
            choices = data.get("choices") or []
            msg = (choices[0].get("message") or {}) if choices else {}
            usage = data.get("usage") or {}
            print(
                f"[openai_compat] generate HTTP {resp.status_code} "
                f"choices={len(choices)} content={msg.get('content','')[:120]!r} "
                f"reasoning={msg.get('reasoning_content','')[:80]!r} "
                f"finish={choices[0].get('finish_reason') if choices else None} "
                f"prompt_tokens={usage.get('prompt_tokens')} "
                f"completion_tokens={usage.get('completion_tokens')} "
                f"reasoning_tokens={usage.get('completion_tokens_details',{}).get('reasoning_tokens')}"
            )
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        text = (message.get("content") or choice.get("text") or "").strip()
        usage = data.get("usage") or {}
        return LLMResponse(
            text=text,
            finish_reason=choice.get("finish_reason") or "stop",
            num_prompt_tokens=int(usage.get("prompt_tokens") or 0),
            num_generated_tokens=int(usage.get("completion_tokens") or 0),
            engine=self.name,
        )

    def stream(self, req: LLMRequest) -> Iterator[str]:
        client = self._get_client()
        url = _chat_url(self._base_url)
        headers = {
            **_auth_headers(self._api_key),
            "Accept": "text/event-stream",
        }
        try:
            with client.stream(
                "POST",
                url,
                json=_payload(
                    req, self._model, stream=True, guided_json=self._guided_json
                ),
                headers=headers,
            ) as resp:
                _raise_http(resp, "stream")
                for data in _iter_sse_data_lines(resp):
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    content = delta.get("content")
                    if content:
                        yield content
        except httpx.RequestError as exc:
            raise RuntimeError(
                f"openai_compat stream request failed: {exc}"
            ) from exc

    def unload(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None


# Alias used in contracts / ops docs (keep canonical name openai_compat).
register_engine("remote")(OpenAICompatEngine)
OpenAICompatEngine.name = "openai_compat"
