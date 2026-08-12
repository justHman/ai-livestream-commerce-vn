"""Change A -> Change T integration smoke (task 16.3).

Change A (`adaptive-speech-text-chunking`) owns speech-text segmentation:
its canonical `TextChunk` (session_id/utterance_id/seq/text/is_final) lives
in `backend_service/backend/application/text_chunker/types.py`. The TTS
service is a separate package that must never import backend_service, so
this test uses the service-local `TextChunk` dataclass (identical field
shape, `tts/engines/base.py`) to stand in for Change A output.

The Change A contract is consumed, not re-implemented: chunk fields map
1:1 onto the ordinary `POST /v1/audio/speech` request fields (there is no
batch endpoint — batching is scheduler-owned), and every submitted chunk
comes back with its utterance/chunk identity intact.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from tts.engines.base import TextChunk

from .test_runtime_api import FakeClock, FakeProvider, _make_runtime, _prepare_app, start_ticking

NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=timezone.utc)


def _as_chunks(utterance: str) -> list[TextChunk]:
    """Stand-in for Change A chunking output: one chunk per sentence."""
    return [
        TextChunk(
            session_id="sess-a", utterance_id="utt-a", seq=0, text="Xin chào.", is_final=False
        ),
        TextChunk(
            session_id="sess-a", utterance_id="utt-a", seq=1, text="Hôm nay bán gì?", is_final=False
        ),
        TextChunk(session_id="sess-a", utterance_id="utt-a", seq=2, text="Giá tốt.", is_final=True),
    ]


def _payload_for(chunk: TextChunk, **overrides) -> dict:
    return {
        "text": chunk.text,
        "session_id": chunk.session_id,
        "utterance_id": chunk.utterance_id,
        "chunk_seq": chunk.seq,
        "response_format": "wav",
        **overrides,
    }


def test_change_a_chunks_flow_through_runtime_with_identity(monkeypatch) -> None:
    clock = FakeClock()
    provider = FakeProvider(max_batch_size=2)  # chunks 0+1 batch, chunk 2 separate
    app = _prepare_app(monkeypatch, provider=provider)
    start_ticking(clock)
    with TestClient(app) as client:
        app.state.runtime = _make_runtime(clock, provider)
        responses = [
            client.post("/v1/audio/speech", json=_payload_for(chunk)) for chunk in _as_chunks("x")
        ]
    assert all(r.status_code == 200 for r in responses)
    # Every chunk produced audio with its identity echoed back.
    for chunk, resp in zip(_as_chunks("x"), responses):
        assert resp.content[:4] == b"RIFF"
        assert resp.headers["x-session-id"] == chunk.session_id
        assert resp.headers["x-utterance-id"] == chunk.utterance_id
        assert resp.headers["x-chunk-seq"] == str(chunk.seq)
    # All three chunks dispatched, none lost or duplicated.
    dispatched = [rid for batch in provider.batch_calls for rid in batch]
    assert len(dispatched) == 3
    assert len(set(dispatched)) == 3
    assert all(r.headers["x-audio-engine"] == "scheduler" for r in responses)


def test_change_a_chunk_maps_to_request_fields_1to1() -> None:
    chunk = TextChunk(
        session_id="sess-a", utterance_id="utt-a", seq=4, text="Chunk bốn.", is_final=True
    )
    payload = _payload_for(chunk)
    assert payload["text"] == "Chunk bốn."
    assert payload["session_id"] == "sess-a"
    assert payload["utterance_id"] == "utt-a"
    assert payload["chunk_seq"] == 4
