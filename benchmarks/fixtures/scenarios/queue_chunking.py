"""Queue/chunking fixture: waveform/text -> chunks.

Records the deterministic ChatQueue rolling-window semantics and text
chunking boundaries the legacy Director depends on: comments ingested at
fixed timestamps, window filtering, eviction order, and the text-chunk
splitting parameters used by the playback path.
"""

from __future__ import annotations

from typing import Any

from core.director.chat_queue import ChatQueue, IncomingComment

from ..corpus import jsonable


def _chunk_text(text: str, target: int, max_chars: int) -> list[str]:
    """Deterministic text chunking (mirror of the playback chunker contract)."""
    words = text.split()
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for word in words:
        if current_len + len(word) + 1 > max_chars and current:
            chunks.append(" ".join(current))
            current = []
            current_len = 0
        current.append(word)
        current_len += len(word) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def scenario() -> dict[str, Any]:
    queue = ChatQueue("sess-queue-1", max_size=500)
    # Fixed clock: comments at t=0..99 (within a 75s window from t=100).
    # Ids pinned for determinism (put() would otherwise assign uuid4 ids).
    for i in range(20):
        queue._deque.append(
            IncomingComment(
                text=f"comment {i}",
                author=f"user{i % 5}",
                ts=float(i * 5),
                id=f"queue-{i:03d}",
            )
        )
        queue._total_put += 1
    snapshot = queue.snapshot(window_sec=75.0, now=100.0)

    # Text chunking over a long VN sales line.
    long_line = (
        "Áo hoodie này chất cotton 100% dày dặn form rộng rất thoải mái "
        "giá chỉ 299 ngàn đồng thôi nha mọi người nhanh tay chốt đơn nhé"
    )
    chunks = _chunk_text(long_line, target=40, max_chars=60)

    inputs = {
        "queue_session": "sess-queue-1",
        "max_size": 500,
        "put_ts": [float(i * 5) for i in range(20)],
        "window_sec": 75.0,
        "now": 100.0,
        "text": long_line,
        "chunk_target_chars": 40,
        "chunk_max_chars": 60,
    }
    outputs = {
        "window_comment_ids": [c.id for c in snapshot],
        "window_texts": [c.text for c in snapshot],
        "window_count": len(snapshot),
        "stats": queue.stats(window_sec=75.0, now=100.0),
        "chunks": chunks,
        "chunk_count": len(chunks),
    }
    return {"inputs": jsonable(inputs), "outputs": jsonable(outputs)}
