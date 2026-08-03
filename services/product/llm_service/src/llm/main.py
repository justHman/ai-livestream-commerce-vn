"""LLM service — self-host vLLM/SGLang/transformers inference.
Entrypoint: uvicorn llm.main:app
"""

from llm.bootstrap.app_factory import create_app

app = create_app()