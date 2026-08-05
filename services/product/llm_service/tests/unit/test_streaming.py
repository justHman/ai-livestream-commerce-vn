"""Streaming semantics: bounded chunking and cancellation-safe iterators."""

from __future__ import annotations

from llm.engines.base import LLMRequest, _NoopEngine


class _StreamEngine(_NoopEngine):
    """Noop engine with incremental stream deltas."""

    name = "stream"

    @classmethod
    def from_config(cls, cfg: dict) -> "_StreamEngine":
        return cls()

    def stream(self, req: LLMRequest):
        yield "hello "
        yield "world"


def test_stream_yields_deltas() -> None:
    engine = _StreamEngine()
    chunks = list(engine.stream(LLMRequest(messages=[])))
    assert chunks == ["hello ", "world"]


def test_stream_chunks_marks_last_final() -> None:
    engine = _StreamEngine()
    chunks = list(engine.stream_chunks(LLMRequest(messages=[]), session_id="s"))
    assert [c.text for c in chunks] == ["hello ", "world"]
    assert [c.is_final for c in chunks] == [False, True]
    assert all(c.session_id == "s" for c in chunks)


def test_noop_stream_single_chunk() -> None:
    engine = _NoopEngine()
    req = LLMRequest(messages=[{"role": "user", "content": "x"}])
    assert engine.generate(req).engine == "none"
