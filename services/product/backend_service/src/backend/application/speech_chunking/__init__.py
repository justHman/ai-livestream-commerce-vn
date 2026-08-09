"""Canonical speech-text chunking types (OpenSpec adaptive-speech-text-chunking).

Source-agnostic: the public contract carries no ``llm``/``script``/``approved``
fields, so segmentation never depends on who produced the text.
"""

from .types import ChunkDecisionReason, ChunkPolicy, RuntimeHints, TextChunk

__all__ = ["TextChunk", "ChunkPolicy", "RuntimeHints", "ChunkDecisionReason"]
