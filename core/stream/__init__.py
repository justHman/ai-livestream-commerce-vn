"""core.stream — streaming pipeline stages between LLM and TTS.

This package holds the text chunker that coalesces token-sized LLM deltas
into phrase-sized TextChunks suitable for TTS. The streaming pipeline is:

  LLM stream (token deltas)
    -> TextChunker (coalesce to phrase-sized TextChunks)
    -> TTS stream
    -> avatar render stream
"""

from __future__ import annotations
