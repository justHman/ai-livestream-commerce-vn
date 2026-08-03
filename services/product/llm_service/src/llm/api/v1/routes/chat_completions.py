"""OpenAI-compatible chat completions route.

Routes resolve the active engine from a dependency and invoke the typed
base interface directly — no pass-through delegation (Task 1.31).
Streaming is bounded; cancellation and cleanup happen in the engine layer.
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from llm.api.dependencies import (
    get_engine,
    get_gpu_concurrency_limiter,
)
from llm.api.security.authorization import require_scope
from llm.api.security.rate_limit import GPUConcurrencyLimiter
from llm.api.v1.schemas.chat import (
    ChatCompletionChunk,
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    StreamChoice,
    Usage,
)
from llm.api.v1.schemas.common import ErrorResponse
from llm.engines.base import LLMEngine, LLMRequest

router = APIRouter()


def _model_id(engine: LLMEngine, requested: str) -> str:
    return requested or engine.name


def _to_llm_request(body: ChatCompletionRequest) -> LLMRequest:
    return LLMRequest(
        messages=[msg.model_dump() for msg in body.messages],
        max_tokens=body.max_tokens,
        temperature=body.temperature,
        top_p=body.top_p,
        top_k=body.top_k,
        stop=list(body.stop),
        seed=body.seed if body.seed is not None else 42,
        repetition_penalty=body.repetition_penalty,
        frequency_penalty=body.frequency_penalty,
    )


@router.post(
    "/chat/completions",
    response_model=ChatCompletionResponse,
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse},
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        502: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
async def chat_completions(
    body: ChatCompletionRequest,
    _scope: str = Depends(require_scope("llm.inference")),
    engine: LLMEngine = Depends(get_engine),
    limiter: GPUConcurrencyLimiter = Depends(get_gpu_concurrency_limiter),
):
    """Serve an OpenAI-compatible chat completion.

    Auth, body limit, rate, and GPU concurrency gates run before any engine
    work. Streams via SSE when `body.stream` is set.
    """
    model = _model_id(engine, body.model)
    if body.stream:
        return await _stream_response(engine, body, model)

    with limiter:
        response = engine.generate(_to_llm_request(body))

    return ChatCompletionResponse(
        id=f"chatcmpl-{int(time.time() * 1000)}",
        created=int(time.time()),
        model=model,
        choices=[
            Choice(
                message=ChatMessage(role="assistant", content=response.text),
                finish_reason=response.finish_reason,
            )
        ],
        usage=Usage(
            prompt_tokens=response.num_prompt_tokens,
            completion_tokens=response.num_generated_tokens,
            total_tokens=response.num_prompt_tokens + response.num_generated_tokens,
        ),
    )


async def _stream_response(
    engine: LLMEngine, body: ChatCompletionRequest, model: str
) -> StreamingResponse:
    """Run the blocking engine stream in a worker thread and forward SSE."""

    async def _event_stream() -> AsyncIterator[str]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        def _pump() -> None:
            try:
                for text in engine.stream(_to_llm_request(body)):
                    obj = ChatCompletionChunk(
                        id=f"chatcmpl-{int(time.time() * 1000)}",
                        created=int(time.time()),
                        model=model,
                        choices=[
                            StreamChoice(
                                index=0,
                                delta={"content": text},
                                finish_reason=None,
                            )
                        ],
                    )
                    queue.put_nowait(obj.model_dump_json())
                obj = ChatCompletionChunk(
                    id=f"chatcmpl-{int(time.time() * 1000)}",
                    created=int(time.time()),
                    model=model,
                    choices=[StreamChoice(index=0, delta={}, finish_reason="stop")],
                )
                queue.put_nowait(obj.model_dump_json())
            except Exception:
                pass
            finally:
                queue.put_nowait(None)

        task = loop.run_in_executor(None, _pump)
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {item}\n\n"
        yield "data: [DONE]\n\n"
        await task

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
