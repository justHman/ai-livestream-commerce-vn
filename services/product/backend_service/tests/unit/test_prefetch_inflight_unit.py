"""A unit popped for playback but not yet speaking must not be decided a second time.

Live test 2026-10-13: the playback loop pops the next prepared turn and then awaits the approved
speech revalidation; the prefetch tick ran in that window, the popped turn was in neither queue,
and the projection decided the same script unit again (the unit was read twice in a row).
"""

import asyncio
from collections import deque
from types import SimpleNamespace

import pytest

from backend.api.v1.router import build_run_plan
from backend.application.director.config import StreamConfig
from backend.application.director.coordinator import DirectorCoordinator, _SessionStats
from backend.application.director.embeddings import HashingEmbedder
from backend.application.director.session_context import DirectorRuntime
from backend.application.director.state import Phase
from tests.unit.test_decision_preparation import _product, _RecordingCloudBackend, _RecordingHub

UNITS = tuple(f"Đoạn script số {i}." for i in range(8))


class _Approval:
    def blocked(self, session_id):
        return None

    async def prepare(self, session_id, text, **kwargs):
        return SimpleNamespace(text=text)


@pytest.mark.asyncio
async def test_unit_popped_for_playback_is_not_decided_again():
    backend, hub = _RecordingCloudBackend(), _RecordingHub()
    runtime = DirectorRuntime(backend=backend, embedder=HashingEmbedder())
    coordinator = DirectorCoordinator(runtime=runtime, llm=None, tts=None, backend=backend, hub=hub)
    coordinator.approved_speech = _Approval()
    sid = "inflight-unit"
    product = _product()
    runtime.attach(
        sid,
        [product.to_entity()],
        cfg=StreamConfig(prepared_turn_depth=2),
        run_plan=build_run_plan([product]),
    )
    director = runtime.get_session(sid).director
    state = director.state
    state.phase = Phase.SELLING
    state.cursor.opening_completed = True
    state.current_product().is_introduced = True
    state.current_product().stage_turn_index = 2
    state.current_product().units = UNITS
    coordinator._stats[sid] = _SessionStats()
    coordinator._decision_queue[sid] = deque()
    coordinator._speech_queue[sid] = deque()
    coordinator._prepare_tasks[sid] = set()
    coordinator._decision_locks[sid] = asyncio.Lock()
    coordinator._playback_events[sid] = asyncio.Event()
    coordinator._completed_history[sid] = deque(maxlen=3)

    async def fill():
        await coordinator._fill_prepared(sid)
        await asyncio.gather(*coordinator._prepare_tasks[sid])

    await fill()
    assert [d.unit_index for d in coordinator._speech_queue[sid]] == [0, 1]

    # The playback loop popped unit 0 and is awaiting the revalidation: stand in for it.
    entered, release = asyncio.Event(), asyncio.Event()
    spoken = []

    async def slow_maybe_speak(session_id, decision):
        spoken.append(decision.unit_index)
        entered.set()
        await release.wait()
        director.mark_spoken(decision)
        coordinator._completed_history[sid].append({"turn_id": decision.turn_id})
        return True

    coordinator._maybe_speak = slow_maybe_speak
    loop_task = asyncio.create_task(coordinator._playback_loop(sid))
    coordinator._playback_events[sid].set()
    await asyncio.wait_for(entered.wait(), 2)
    assert spoken == [0]

    await fill()  # the prefetch tick while unit 0 is popped but not speaking yet
    queued = [d.unit_index for d in coordinator._speech_queue[sid]]
    assert queued == [1, 2], queued  # never unit 0 again

    release.set()
    loop_task.cancel()
    await asyncio.gather(loop_task, return_exceptions=True)
