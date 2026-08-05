"""Unit tests for LLM streaming (Task 2).

Covers:
  - LLMEngine.stream_chunks() default behavior on _NoopEngine + a stub engine
    yielding fixed deltas (seq increments, is_final on last, text concatenates).
  - TextChunk fields populated (id non-empty, session_id/utterance_id passed through).
  - LlamaCppEngine.stream_chunks() incremental override using a fake Llama (no
    real GGUF / no network) — exercises the one-ahead buffer path.
  - Missing GGUF path raises a clear error (FileNotFoundError if llama_cpp importable,
    or the import error if llama_cpp is not installed). Deterministic, offline.

Runs fully offline; no model downloads.
"""

from __future__ import annotations

from typing import Iterator

import pytest

from llm.engines.base import LLMEngine, LLMRequest, _NoopEngine
from llm.engines.base import TextChunk


# ---------- helpers / stubs ----------


class _StubEngine(LLMEngine):
    """Minimal LLMEngine whose stream() yields fixed deltas. Used to exercise the
    default stream_chunks() implementation."""

    name = "stub"

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = list(deltas)

    @classmethod
    def from_config(cls, cfg: dict) -> "_StubEngine":  # pragma: no cover - unused here
        return cls([])

    def generate(self, req: LLMRequest):  # pragma: no cover - unused here
        raise RuntimeError("stub: use stream(), not generate()")

    def stream(self, req: LLMRequest) -> Iterator[str]:
        for d in self._deltas:
            yield d


class _FakeLlama:
    """Fake of llama_cpp.Llama that emits deterministic delta dicts from a list.
    Mimics the create_chat_completion(stream=True) output shape."""

    def __init__(self, deltas: list[str]) -> None:
        self._deltas = list(deltas)

    def create_chat_completion(
        self, *, messages, max_tokens, temperature, top_p, stop, seed, stream, **kwargs
    ):
        if not stream:
            full = "".join(self._deltas)
            return {
                "choices": [{"message": {"content": full}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": len(self._deltas)},
            }
        for d in self._deltas:
            yield {"choices": [{"delta": {"content": d}, "finish_reason": None}]}
        # Final terminator chunk (no content) — matches llama-cpp-python behavior.
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}


# ---------- default stream_chunks() on _NoopEngine ----------


def test_noop_stream_chunks_yields_at_least_one_final_chunk():
    """_NoopEngine.stream_chunks() must yield >=1 TextChunk with the last is_final=True."""
    e = _NoopEngine()
    req = LLMRequest.from_prompt("hello")
    chunks = list(e.stream_chunks(req, session_id="s1", utterance_id="u1"))
    assert len(chunks) >= 1
    assert all(isinstance(c, TextChunk) for c in chunks)
    assert chunks[-1].is_final is True
    # _NoopEngine.generate returns "[noop] <user>"; the stream default yields it as one delta.
    assert chunks[0].text.startswith("[noop]")


def test_noop_stream_chunks_str_stream_unchanged():
    """The existing stream() -> Iterator[str] must still work (not broken by stream_chunks)."""
    e = _NoopEngine()
    req = LLMRequest.from_prompt("hi")
    out = list(e.stream(req))
    assert isinstance(out[0], str)
    assert out[0] == "[noop] hi"


# ---------- default stream_chunks() on a stub with fixed deltas ----------


def test_stream_chunks_default_per_delta_seq_and_final():
    """Stub stream() yields ["Xin ", "chào ", "bạn."] -> stream_chunks() yields 3 TextChunks
    with seq 0,1,2; last is_final=True; concatenated text equals "Xin chào bạn."."""
    e = _StubEngine(["Xin ", "chào ", "bạn."])
    req = LLMRequest.from_prompt("greet")
    chunks = list(e.stream_chunks(req, session_id="sess-7", utterance_id="utt-9"))

    assert len(chunks) == 3
    assert [c.seq for c in chunks] == [0, 1, 2]
    assert [c.is_final for c in chunks] == [False, False, True]
    assert "".join(c.text for c in chunks) == "Xin chào bạn."


def test_stream_chunks_propagates_session_and_utterance_ids():
    """TextChunk.session_id / utterance_id must equal the kwargs passed in."""
    e = _StubEngine(["a", "b"])
    req = LLMRequest.from_prompt("x")
    chunks = list(e.stream_chunks(req, session_id="S", utterance_id="U"))
    for c in chunks:
        assert c.session_id == "S"
        assert c.utterance_id == "U"


def test_stream_chunks_ids_non_empty_and_unique():
    """Every TextChunk.id must be non-empty and unique within the stream."""
    e = _StubEngine(["a", "b", "c"])
    req = LLMRequest.from_prompt("x")
    chunks = list(e.stream_chunks(req, session_id="s", utterance_id="u"))
    ids = [c.id for c in chunks]
    assert all(len(i) > 0 for i in ids)
    assert len(set(ids)) == len(ids)


def test_stream_chunks_single_delta_is_final():
    """A one-delta stream yields exactly one TextChunk with is_final=True."""
    e = _StubEngine(["only"])
    req = LLMRequest.from_prompt("x")
    chunks = list(e.stream_chunks(req, session_id="s", utterance_id="u"))
    assert len(chunks) == 1
    assert chunks[0].is_final is True
    assert chunks[0].text == "only"
    assert chunks[0].seq == 0


def test_stream_chunks_empty_stream_yields_nothing():
    """An empty stream yields zero chunks (no spurious final marker)."""
    e = _StubEngine([])
    req = LLMRequest.from_prompt("x")
    chunks = list(e.stream_chunks(req, session_id="s", utterance_id="u"))
    assert chunks == []


# ---------- LlamaCppEngine.stream_chunks() incremental override ----------


def test_llamacpp_stream_chunks_incremental_with_fake_llama(monkeypatch):
    """Exercise LlamaCppEngine.stream_chunks() without a real GGUF: inject a fake
    Llama instance and verify incremental TextChunks (one per delta, last is_final)."""
    from llm.engines.llamacpp import LlamaCppEngine as mod

    e = mod()
    e._llm = _FakeLlama(["Xin ", "chào ", "bạn."])
    e._system_prompt = None
    e.name = "llamacpp"

    req = LLMRequest.from_prompt("greet")
    chunks = list(e.stream_chunks(req, session_id="ls", utterance_id="lu"))

    assert len(chunks) == 3
    assert [c.seq for c in chunks] == [0, 1, 2]
    assert [c.is_final for c in chunks] == [False, False, True]
    assert "".join(c.text for c in chunks) == "Xin chào bạn."
    for c in chunks:
        assert c.session_id == "ls"
        assert c.utterance_id == "lu"


def test_llamacpp_stream_chunks_skips_empty_deltas(monkeypatch):
    """The llamacpp stream() skips empty/None content; stream_chunks must too —
    no TextChunk with empty text should be emitted."""
    from llm.engines.llamacpp import LlamaCppEngine as mod

    e = mod()
    # _FakeLlama with an empty delta in the middle: stream() yields only non-empty ones.
    e._llm = _FakeLlama(["a", "", "b"])
    e._system_prompt = None
    e.name = "llamacpp"

    req = LLMRequest.from_prompt("x")
    chunks = list(e.stream_chunks(req, session_id="s", utterance_id="u"))
    # The empty middle delta is filtered out by stream(), so we get 2 chunks.
    assert len(chunks) == 2
    assert "".join(c.text for c in chunks) == "ab"
    assert chunks[-1].is_final is True


# ---------- missing GGUF path deterministic error ----------


def test_llamacpp_missing_gguf_raises_clear_error(tmp_path):
    """Building a llamacpp engine with a non-existent model_path and no model repo
    must raise a clear error. Handles two cases:
      - llama_cpp NOT installed: the `from llama_cpp import Llama` inside from_config
        raises ImportError — assert message mentions llama_cpp / llama-cpp-python.
      - llama_cpp IS installed: pass a bogus path -> FileNotFoundError.
    Deterministic, offline, no network."""
    try:
        import llama_cpp  # noqa: F401

        llama_available = True
    except ImportError:
        llama_available = False

    from llm.engines.llamacpp import LlamaCppEngine

    bogus = str(tmp_path / "does_not_exist.gguf")
    if llama_available:
        with pytest.raises((FileNotFoundError, ValueError)) as excinfo:
            LlamaCppEngine.from_config({"engine": "llamacpp", "model_path": bogus})
        # Error should reference the path or the missing gguf.
        msg = str(excinfo.value)
        assert bogus in msg or ".gguf" in msg, msg
    else:
        with pytest.raises(ImportError) as excinfo:
            LlamaCppEngine.from_config({"engine": "llamacpp", "model_path": bogus})
        msg = str(excinfo.value).lower()
        assert "llama" in msg, msg


@pytest.mark.skip(reason="integration: needs Qwen3.5 GGUF on disk + llama_cpp installed")
def test_llamacpp_real_gguf_load_and_stream():
    """Integration marker: would load a real Qwen3.5-4B-Q4_K_M.gguf and stream."""
    pass


def test_llamacpp_bogus_file_path_raises_filenotfounderror(tmp_path):
    """A non-existent local file path (not a dir, not empty, no model_repo) must raise
    FileNotFoundError naming the expected path — BEFORE Llama() is called, so the
    error is deterministic and does not depend on llama_cpp internals.

    Skipped when llama_cpp is not importable (from_config imports Llama at the top
    of the method, so ImportError fires before the validation). Mirrors the
    skip-when-llama_cpp-absent guard of test_llamacpp_missing_gguf_raises_clear_error.
    """
    try:
        import llama_cpp  # noqa: F401

        llama_available = True
    except ImportError:
        llama_available = False

    if not llama_available:
        pytest.skip("llama_cpp not installed; from_config raises ImportError before validation")

    from llm.engines.llamacpp import LlamaCppEngine

    bogus = str(tmp_path / "does_not_exist.gguf")
    with pytest.raises(FileNotFoundError) as excinfo:
        LlamaCppEngine.from_config({"engine": "llamacpp", "model_path": bogus})
    msg = str(excinfo.value)
    assert bogus in msg, msg
