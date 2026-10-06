"""A rejected question must leave other real Director selections eligible."""

import asyncio
import time
from collections import deque
from types import SimpleNamespace

import pytest

from backend.api.v1.router import build_run_plan
from backend.application.director.clustering import Comment
from backend.application.director.config import StreamConfig
from backend.application.director.coordinator import DirectorCoordinator, _SessionStats
from backend.application.director.decision import Decision
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.session_context import DirectorRuntime
from backend.application.director.state import Phase
from backend.application.reducer.fast_reducer import AcceptedComment, FastReducer, FastReducerConfig
from backend.application.script_authoring.approved_speech import SpeechRejected
from tests.unit.test_decision_preparation import _product, _RecordingCloudBackend, _RecordingHub


class _SelectiveApproval:
    selective = False

    def __init__(self):
        self.calls = []

    def blocked(self, session_id):
        return None

    async def prepare(self, session_id, text, **kwargs):
        self.calls.append(text)
        if not self.selective or "giá bao nhiêu" in text:
            raise SpeechRejected("unsupported_content")
        return SimpleNamespace(text="approved speech")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy", "reducer"])
async def test_backed_off_price_question_leaves_stock_and_sales_eligible(mode):
    backend, hub = _RecordingCloudBackend(), _RecordingHub()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    reducer = FastReducer(config=FastReducerConfig(), embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(
        runtime=runtime, llm=None, tts=None, backend=backend, hub=hub, reducer=reducer
    )
    approval = _SelectiveApproval()
    coordinator.approved_speech = approval
    clock = [1000.0]
    coordinator._monotonic = lambda: clock[0]
    sid = "rejected-selection"
    product = _product(features=["ấm", "mềm", "bền"], price=350000)
    runtime.attach(
        sid,
        [product.to_entity()],
        cfg=StreamConfig(prepared_turn_depth=3),
        run_plan=build_run_plan([product]),
    )
    session = runtime.get_session(sid)
    state = session.director.state
    state.phase = Phase.SELLING
    state.cursor.opening_completed = True
    state.current_product().is_introduced = True
    state.current_product().stage_turn_index = 2
    coordinator._stats[sid] = _SessionStats()
    coordinator._decision_queue[sid] = deque()
    coordinator._speech_queue[sid] = deque()
    coordinator._prepare_tasks[sid] = set()
    coordinator._decision_locks[sid] = asyncio.Lock()
    coordinator._playback_events[sid] = asyncio.Event()
    coordinator._completed_history[sid] = deque(maxlen=3)
    if mode == "reducer":
        coordinator.set_reducer_mode(sid)
        coordinator.mark_reducer_ready(sid)

    async def seed(prefix, text, intent, vector, count):
        for index in range(count):
            cid = f"{prefix}{index}"
            if mode == "reducer":
                reducer.notify_new_events(
                    sid,
                    comment=AcceptedComment(
                        event_id="event-" + cid,
                        comment_id=cid,
                        text=text,
                        ts=time.time(),
                        viewer_key=cid,
                        provenance={},
                    ),
                )
            else:
                state.rolling_comments.append(
                    Comment(
                        text=text,
                        embedding=vector,
                        t=session.now(),
                        id=cid,
                        intent=intent,
                        product_id="P004",
                    )
                )
        if mode == "reducer":
            await reducer.run_once(sid, time.time())

    async def fill():
        await coordinator._fill_prepared(sid)
        await asyncio.gather(*coordinator._prepare_tasks[sid])
        await asyncio.sleep(0)

    await seed("price-", "giá bao nhiêu", "price", [1.0, 0.0], 4)
    await fill()
    assert any(e.get("action", "").startswith("answer") for e in hub.events)
    hub.events.clear()
    approval.calls.clear()
    approval.selective = True
    await seed("stock-", "còn hàng không", "stock", [0.0, 1.0], 2)
    answered_before = set(state.answered_comments)
    await fill()
    prepared = list(coordinator._speech_queue[sid])
    assert any(
        d.action.startswith("answer") and "stock-0" in d.cluster_member_ids for d in prepared
    )
    assert any(d.action == "sell_product" for d in prepared)
    assert not any("price-0" in d.cluster_member_ids for d in prepared)
    assert not any("giá bao nhiêu" in text for text in approval.calls)
    assert state.answered_comments == answered_before  # rejection never fabricates spoken evidence
    assert {c.id for c in state.rolling_comments} >= (
        {"price-0", "stock-0"} if mode == "legacy" else set()
    )  # temporary selection filtering does not delete real comments

    # After its own deadline the rejected price question is selectable again.
    coordinator._speech_queue[sid].clear()
    coordinator._decision_queue[sid].clear()
    clock[0] += 61
    hub.events.clear()
    approval.calls.clear()
    await fill()
    assert any("giá bao nhiêu" in text for text in approval.calls)


def test_long_lived_rejection_counter_keeps_the_delay_capped():
    backend = _RecordingCloudBackend()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend)
    coordinator._monotonic = lambda: 1000.0
    for _ in range(2000):
        coordinator._note_rejected("long-live", Decision(action="close"))
    assert coordinator._backoff[("long-live", "close")][1] == 1060.0
