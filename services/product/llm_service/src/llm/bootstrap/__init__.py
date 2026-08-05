"""LLM service bootstrap — app factory and bounded lifespan."""

from llm.bootstrap.app_factory import create_app
from llm.bootstrap.lifespan import create_lifespan

__all__ = ["create_app", "create_lifespan"]
