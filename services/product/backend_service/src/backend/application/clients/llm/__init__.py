"""LLM outbound client (OpenAI-compatible self-host or hosted endpoint)."""

from backend.application.clients.llm.openai_compatible import (
    ChatMessage,
    ChatRequest,
    LLMClientError,
    LLMResult,
    OpenAICompatibleClient,
)

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "LLMClientError",
    "LLMResult",
    "OpenAICompatibleClient",
]
