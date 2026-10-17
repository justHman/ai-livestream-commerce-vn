"""M9(b): the log lines that make the comment -> answer delay measurable on the next live."""

from __future__ import annotations

import logging
import time

from backend.application.director.coordinator import _decision_to_start_ms
from backend.application.director.decision import Decision
from backend.application.director.embeddings import HashingEmbedder
from backend.application.reducer import AcceptedComment, FastReducer, FastReducerConfig


def test_decision_to_start_is_the_gap_between_deciding_and_the_audio_starting() -> None:
    decision = Decision(action="answer_cluster")
    decision.latency_spans["decision"] = {"start": 100.0, "end": 100.0}
    assert _decision_to_start_ms(decision, 112.5) == 12500


def test_decision_to_start_is_na_when_the_decision_time_is_unknown() -> None:
    assert _decision_to_start_ms(Decision(action="answer_cluster"), 5.0) == "na"


def test_reducer_logs_how_old_a_new_comment_already_is_but_not_on_redelivery(caplog) -> None:
    reducer = FastReducer(config=FastReducerConfig(), embedder=HashingEmbedder())
    old = time.time() - 20.0  # the comment was posted 20 s ago on the platform
    item = AcceptedComment(
        event_id="e1",
        comment_id="c1",
        text="Giá bao nhiêu?",
        ts=old,
        viewer_key="v1",
        provenance={},
    )
    with caplog.at_level(logging.INFO, logger="backend.application.reducer.fast_reducer"):
        reducer.notify_new_events("s1", comment=item)
        reducer.notify_new_events("s1", comment=item)  # redelivery of the same comment
    lines = [r.getMessage() for r in caplog.records if "comment accepted" in r.getMessage()]
    assert len(lines) == 1
    lag = int(lines[0].rsplit("source_lag_ms=", 1)[1])
    assert 19_000 <= lag <= 25_000
