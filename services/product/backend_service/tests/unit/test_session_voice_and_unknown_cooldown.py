"""Live speech uses the session voice; unknown-intent comments are not swallowed by cooldown."""

from __future__ import annotations

import logging
from types import SimpleNamespace

from backend.application.director.coordinator import DirectorCoordinator
from backend.application.director.decision import Comment, Director
from backend.application.director.config import StreamConfig
from backend.application.director.state import Phase, ProductState, StreamState


class _Store:
    def __init__(self, meta):
        self.meta = meta

    async def get(self, sid):
        return self.meta


class _TTS:
    def __init__(self):
        self.reqs = []

    def stream_audio(self, chunk, **kw):
        self.reqs.append(kw.get("req"))
        yield from ()


class _EM:
    def __init__(self, ids):
        self.ids = ids

    def voices(self):
        return [{"id": i, "label": i, "active": False} for i in self.ids]


def _coord(meta, em=None):
    tts = _TTS()
    c = DirectorCoordinator(
        runtime=None, llm=None, tts=tts, backend=None, session_store=_Store(meta), engine_manager=em
    )
    return c, tts


async def test_session_voice_applied():
    c, tts = _coord({"voice_id": "Trúc Ly"}, _EM(["Hải Đăng", "Trúc Ly"]))
    wrapped = await c._session_tts("s")
    list(wrapped.stream_audio(SimpleNamespace(text="xin chao")))
    assert tts.reqs[-1].voice == "Trúc Ly"


async def test_no_voice_keeps_default_engine():
    c, tts = _coord({})
    assert await c._session_tts("s") is tts
    c, tts = _coord(None)
    assert await c._session_tts("s") is tts


async def test_unknown_voice_falls_back_with_warning(caplog):
    c, tts = _coord({"voice_id": "Ghost"}, _EM(["Hải Đăng"]))
    with caplog.at_level(logging.WARNING):
        assert await c._session_tts("s") is tts
    assert "session voice not available, using default" in caplog.text


async def test_store_failure_falls_back():
    c, tts = _coord({})

    async def boom(sid):
        raise RuntimeError("down")

    c._session_store.get = boom
    assert await c._session_tts("s") is tts


def _director(cooldown=120.0):
    state = StreamState(
        phase=Phase.SELLING,
        products=[ProductState(product_id="P1", name="Ao", is_introduced=True, stage_turn_index=2)],
    )
    d = Director(
        state=state, cfg=StreamConfig(qa_min_cluster_size=1, qa_topic_cooldown_sec=cooldown)
    )
    d.state.qa_window_open = True
    d.state.qa_window_started_at = 0.0
    d.state.qa_window_stage_index = 2
    return d


def _unknown(i, t, vec):
    return Comment(
        id=f"u{i}", text=f"cau la {i}", embedding=vec, t=t, intent="unknown", product_id="P1"
    )


def test_unknown_comment_after_answer_is_still_considered():
    d = _director()
    c1 = [_unknown(1, 10.0, [1.0, 0.0])]
    first = d.decide(c1, now=10.5)
    assert first.action in {"answer_fact", "answer_cluster"}
    d.mark_spoken(first)
    assert d.state.topic_cooldown_until["P1:unknown"] <= 10.5 + 15.0 + 1
    c2 = c1 + [_unknown(2, 11.0, [1.0, 0.0])]
    second = d.decide(c2, now=11.5)
    assert second.action in {"answer_fact", "answer_cluster"}


def test_unknown_same_comment_is_rate_limited_and_logged(caplog):
    d = _director()
    c1 = [_unknown(1, 10.0, [1.0, 0.0])]
    d.mark_spoken(d.decide(c1, now=10.5))
    with caplog.at_level(logging.INFO):
        dup = Comment(
            id="u9",
            text=c1[0].text,
            embedding=[1.0, 0.0],
            t=10.8,
            intent="unknown",
            product_id="P1",
        )
        again = d.decide(c1 + [dup], now=11.0)
    assert again.action not in {"answer_fact", "answer_cluster"}
    assert "comment skipped reason=topic_cooldown topic=P1:unknown remaining_s=" in caplog.text


def test_known_intent_cooldown_unchanged():
    d = _director()
    price = [
        Comment(id="p1", text="gia", embedding=[1.0, 0.0], t=10.0, intent="price", product_id="P1")
    ]
    d.mark_spoken(d.decide(price, now=10.5))
    assert d.state.topic_cooldown_until["P1:price"] >= 10.5 + 100
