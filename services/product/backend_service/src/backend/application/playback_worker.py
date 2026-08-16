"""Canonical playback worker (OpenSpec 1.21).

Coordinates LLM -> chunking -> TTS -> avatar execution for one prepared
Director decision. Owns the pipeline sequencing; the media engines live in
the llm/tts/avatar services and the avatar/voice clients. The legacy
``core.render.orchestrator.StreamOrchestrator`` remains the executed
pipeline until Task 1.26; this module is the canonical home for the
playback policy that the orchestrator wires into.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .playback_queue import PlaybackCancelled, PlaybackQueue, new_item
from .text_chunker import ChunkPolicy, FixedChunkPolicyConfig, TextChunker

__all__ = [
    "PlaybackWorker",
    "PlaybackWorkerConfig",
    "PlaybackCancelled",
    "PlaybackQueue",
    "TextChunker",
]


@dataclass
class PlaybackWorkerConfig:
    """Tunable playback pipeline knobs.

    The character thresholds source their defaults from the canonical
    ``FixedChunkPolicyConfig`` (one typed source; no duplicated defaults).
    ``chunk_policy`` is the single typed runtime policy seam: adaptive_vi by
    default, ``fixed`` the explicit rollback. The realtime flush deadline is
    owned by ``StreamingControllerConfig`` at the orchestration boundary,
    not here.
    """

    min_chars: int = FixedChunkPolicyConfig().min_chars
    target_chars: int = FixedChunkPolicyConfig().target_chars
    max_chars: int = FixedChunkPolicyConfig().max_chars
    chunk_policy: ChunkPolicy | str = ChunkPolicy.ADAPTIVE_VI
    max_queue_windows: int = 5
    transient_retry_count: int = 1


@dataclass
class PlaybackWorker:
    """One session's LLM -> chunker -> TTS -> avatar playback executor.

    Thin orchestration over the injected engines/backends:
      - builds a fresh chunker per utterance,
      - pushes windows into a bounded PlaybackQueue (backpressure),
      - cooperates with cancellation via the queue's cancel flag.
    """

    config: PlaybackWorkerConfig = field(default_factory=PlaybackWorkerConfig)

    def chunker(self, session_id: str, utterance_id: str) -> TextChunker:
        """Fresh chunker for one utterance."""
        policy = (
            self.config.chunk_policy
            if isinstance(self.config.chunk_policy, ChunkPolicy)
            else ChunkPolicy(self.config.chunk_policy)
        )
        return TextChunker(
            session_id=session_id,
            utterance_id=utterance_id,
            min_chars=self.config.min_chars,
            target_chars=self.config.target_chars,
            max_chars=self.config.max_chars,
            policy=policy,
        )

    def queue(self, session_id: str) -> PlaybackQueue:
        """Fresh bounded queue for one turn."""
        return PlaybackQueue(session_id, max_windows=self.config.max_queue_windows)

    def new_queue_item(self, turn_id: str, seq: int, payload: Any, *, is_final: bool = False):
        return new_item(turn_id, seq, payload, is_final=is_final)

    def schedule_retry(self, attempt: int) -> float:
        """Bounded exponential backoff for transient playback failures."""
        if attempt >= self.config.transient_retry_count:
            return 0.0
        return min(0.1 * (2**attempt), 0.5)
