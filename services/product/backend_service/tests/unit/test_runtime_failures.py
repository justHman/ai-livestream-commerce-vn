"""020 R1: recovery, terminal state, stale faults and database-independent fencing."""

import asyncio
import uuid
from types import SimpleNamespace

from backend.api.v1 import execution as ex
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import ExecutionState
from backend.application.runtime_failures import FailureSettings, RuntimeFailures


async def fixture():
    store = InMemorySessionStore()
    owners, blocked, cancelled, invalidated = {}, {}, [], []

    def invalidate(sid):
        owners[sid].generation_token += "!"

    d = SimpleNamespace(
        backend=SimpleNamespace(interrupt=lambda sid: None),
        orchestrators={},
        store=store,
        usage_evidence=None,
        usage_sender=None,
        approved_speech=SimpleNamespace(
            set_runtime_failed=lambda *args: None,
            block=lambda sid, reason: blocked.update({sid: reason}),
            cancel=cancelled.append,
            set_lease_expiry=lambda *args: None,
        ),
        coordinator=SimpleNamespace(
            _runtime=SimpleNamespace(_sessions=owners, invalidate_generation=invalidate),
            _activated=set(),
            _invalidate_queued=lambda sid, reason: invalidated.append(sid),
        ),
    )
    service = RuntimeFailures(
        d, FailureSettings(enabled=True, unhealthy_ticks=2, terminal_ticks=4, attempt_timeout=0.01)
    )
    for sid in ("A", "B"):
        owners[sid] = SimpleNamespace(generation_token="initial")
        d.coordinator._activated.add(sid)
        state = ExecutionState(
            tenant_id=str(uuid.uuid4()),
            business_session_id="business",
            runtime_session_id=sid,
            generation="g",
            phase="selling",
            sequence=3,
        )
        meta = {"execution_contract": state.model_dump(mode="json")}
        await store.set(sid, meta)
        service.register(sid, meta)
    return d, service, owners, blocked, cancelled, invalidated


async def test_transient_ticks_recover_and_persistent_errors_stop_truthfully(monkeypatch):
    d, service, owners, blocked, cancelled, invalidated = await fixture()
    completions = []

    async def complete(sid):
        completions.append(sid)
        return False  # retain the real saved execution for assertion

    monkeypatch.setattr(service.completion, "_complete", complete)
    for _ in range(2):
        service.tick("A", ConnectionError("sensitive provider text"))
    await service.sweep()
    assert (await d.store.get("A"))["execution_contract"]["healthy"] is False
    service.tick("A", None)
    await service.sweep()
    assert (await d.store.get("A"))["execution_contract"]["healthy"] is True
    assert blocked == {} and completions == []
    for _ in range(4):
        service.tick("B", RuntimeError("sensitive provider text"))
    assert blocked == {"B": "ending"} and cancelled == ["B"] and invalidated == ["B"]
    assert "B" not in d.coordinator._activated
    # No durable FAILED claim before the atomic evidence save.
    assert (await d.store.get("B"))["execution_contract"]["phase"] == "selling"
    await service.sweep()
    meta = await d.store.get("B")
    assert meta["execution_contract"]["phase"] == "failed"
    assert meta["execution_failure_class"] == "runtime_error"
    assert meta["execution_contract"]["terminal_reason"] == "execution_failed"
    assert completions == ["B"]


async def test_stale_provider_completion_and_legacy_failure_cannot_kill_a_session():
    d, service, owners, blocked, cancelled, invalidated = await fixture()
    service.fail("A", RuntimeError(), "old-revision")
    service.fail("legacy", RuntimeError())
    assert blocked == {} and cancelled == [] and invalidated == []
    assert (await d.store.get("A"))["execution_contract"]["phase"] == "selling"


async def test_generation_replaced_while_failure_save_retries_is_untouched(monkeypatch):
    d, service, owners, blocked, cancelled, invalidated = await fixture()
    service.fail("A", RuntimeError(), "initial")
    meta = await d.store.get("A")
    meta["execution_contract"]["generation"] = "new-generation"
    owners["A"] = SimpleNamespace(generation_token="initial")
    await d.store.set("A", meta)
    await service.sweep()
    assert (await d.store.get("A"))["execution_contract"]["phase"] == "selling"
    assert service.health()["failures_saved"] == 0


async def test_hanging_database_does_not_delay_fencing_other_faults(monkeypatch):
    d, service, owners, blocked, cancelled, invalidated = await fixture()
    release = asyncio.Event()
    tasks = set()

    class Evidence:
        unstaged_sessions = set()

        def defer_entries(self, prior, updated, cause):
            return [{"test": "deferred terminal fact"}]

    d.usage_evidence = Evidence()

    async def complete(sid):
        tasks.add(asyncio.current_task())
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    monkeypatch.setattr(service.completion, "_complete", complete)
    try:
        service.fail("A", RuntimeError(), "initial")
        service.fail("B", RuntimeError(), "initial")
        assert blocked == {"A": "ending", "B": "ending"}
        await asyncio.wait_for(service.sweep(), 1)
        for sid in ("A", "B"):
            meta = await d.store.get(sid)
            assert meta["execution_contract"]["phase"] == "failed"
            assert meta[ex.UNSTAGED_KEY]
            assert sid in service.completion.tracked
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_real_tick_loop_calls_detector_on_error_and_recovery(monkeypatch):
    from backend.application.director.coordinator import CoordinatorConfig, DirectorCoordinator

    coordinator = object.__new__(DirectorCoordinator)
    coordinator._cfg = CoordinatorConfig(tick_ms=0)
    observed = []
    errors = iter([RuntimeError("hidden text"), None, asyncio.CancelledError()])

    async def tick_once(sid):
        error = next(errors)
        if error is not None:
            raise error

    coordinator._tick_once = tick_once
    coordinator.runtime_failures = SimpleNamespace(tick=lambda sid, error: observed.append(error))
    await coordinator._tick_loop("A")
    assert isinstance(observed[0], RuntimeError)
    assert observed[1] is None and len(observed) == 2


async def test_disabled_wiring_ignores_thresholds_and_creates_nothing(monkeypatch):
    from backend.bootstrap.lifespan import _start_runtime_failures

    monkeypatch.delenv("RUNTIME_FAILURE_DETECTION_ENABLED", raising=False)
    monkeypatch.setenv("RUNTIME_UNHEALTHY_TICKS", "invalid")
    container = SimpleNamespace()
    await _start_runtime_failures(container)
    assert vars(container) == {}


async def test_enabled_wiring_requires_terminal_store(monkeypatch):
    import pytest
    from backend.bootstrap.lifespan import _start_runtime_failures

    monkeypatch.setenv("RUNTIME_FAILURE_DETECTION_ENABLED", "1")
    container = SimpleNamespace(
        coordinator=object(), approved_speech=object(), terminal_outcomes=None
    )
    with pytest.raises(RuntimeError, match="durable terminal"):
        await _start_runtime_failures(container)
    assert not hasattr(container, "runtime_failures")


async def test_real_preparation_fault_is_failed_but_stale_completion_is_cancelled(monkeypatch):
    from collections import deque
    from backend.application.director.coordinator import DirectorCoordinator
    from backend.application.director.decision import Decision

    d, service, owners, blocked, cancelled, invalidated = await fixture()
    runtime = d.coordinator._runtime
    runtime.get_session = lambda sid: owners[sid]
    runtime.prompt_layers = lambda *args: {}
    coordinator = DirectorCoordinator(runtime, None, None, d.backend)
    coordinator.approved_speech = d.approved_speech
    coordinator.runtime_failures = service
    d.coordinator = coordinator
    coordinator._activated.update({"A", "B"})
    for sid in ("A", "B"):
        decision = Decision("speak_hook", text="approved", revision_token="initial")
        coordinator._decision_queue[sid] = deque([decision])

        async def prepare(sid, decision):
            if sid == "B":
                owners[sid].generation_token = "new-revision"
            raise PermissionError("sensitive provider text")

        monkeypatch.setattr(coordinator, "_prepare_approved", prepare)
        await coordinator._prepare_turn(sid, decision)
        assert decision.is_cancelled is (sid == "B")
    assert blocked == {"A": "ending"}
    assert "B" in coordinator._activated
    await service._record("A", service.pending["A"])
    assert (await d.store.get("A"))["execution_contract"]["phase"] == "failed"
    assert (await d.store.get("B"))["execution_contract"]["phase"] == "selling"


async def test_real_completion_retains_ordered_usage_facts_during_outage(monkeypatch):
    from dataclasses import replace
    from backend.api.v1 import sessions
    from backend.application.usage_evidence import UNSTAGED_KEY
    from .test_usage_evidence_m2 import MemOutbox, wire

    d, service, owners, blocked, cancelled, invalidated = await fixture()
    service.settings = replace(service.settings, attempt_timeout=1)
    outbox = MemOutbox()
    outbox.fail.add("*")
    d.usage_evidence, d.usage_sender = wire(d.store, outbox)
    d.coordinator.has = lambda sid: False
    d.backend.stop = lambda sid: None
    d.hub = None
    d.locks = SimpleNamespace(drop=lambda sid: None)
    persisted = []

    async def persist(container, sid):
        persisted.append(sid)  # isolate 019, exercise the real 017 stop/retain path

    monkeypatch.setattr(sessions, "teardown_then_persist", persist)
    for sid in ("A", "B"):
        service.fail(sid, RuntimeError(), "initial")
    await service.sweep()
    assert sorted(persisted) == ["A", "B"]
    for sid in ("A", "B"):
        meta = await d.store.get(sid)
        assert meta[UNSTAGED_KEY] and meta["usage_evidence_cleanup"]
        assert "execution_runtime_failure" not in meta  # no repeated teardown
    await service.sweep()
    assert sorted(persisted) == ["A", "B"]
    outbox.fail.clear()
    await d.usage_sender.sweep()
    assert await d.store.get("A") is None and await d.store.get("B") is None
    assert len(outbox.rows) == 2 and all(row["status"] == "ready" for row in outbox.rows.values())


async def test_revision_change_after_accepted_fault_cannot_erase_failed_outcome():
    d, service, owners, blocked, cancelled, invalidated = await fixture()
    service.fail("A", RuntimeError(), "initial")
    owners["A"].generation_token += "rescue-interrupt"
    await service._record("A", service.pending["A"])
    assert (await d.store.get("A"))["execution_contract"]["phase"] == "failed"


async def test_detach_after_accepted_fault_does_not_erase_current_execution_fact():
    d, service, owners, blocked, cancelled, invalidated = await fixture()
    service.fail("A", RuntimeError(), "initial")
    owners.pop("A")
    await service._record("A", service.pending["A"])
    assert (await d.store.get("A"))["execution_contract"]["phase"] == "failed"


async def test_hanging_discovery_row_cannot_block_tracked_cleanup(monkeypatch):
    d, service, owners, blocked, cancelled, invalidated = await fixture()
    service.fail("A", RuntimeError(), "initial")
    await service._record("A", service.pending.pop("A"))
    original = d.store.get
    completions = []
    release = asyncio.Event()
    tasks = set()
    entered = asyncio.Event()
    cancellation_seen = asyncio.Event()

    async def get(sid):
        if sid == "B":
            tasks.add(asyncio.current_task())
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
                raise
        return await original(sid)

    async def complete(sid):
        completions.append(sid)
        return False

    monkeypatch.setattr(d.store, "get", get)
    monkeypatch.setattr(service.completion, "_complete", complete)
    sweep = asyncio.create_task(service.sweep())
    try:
        # Synchronize on the actual stalled read. The outer guard is a test
        # deadlock bound, not a production latency SLA; loaded CI can pause
        # the event loop longer than 200 ms. Completion must still precede
        # release of cancellation-resistant discovery.
        await asyncio.wait_for(entered.wait(), timeout=2)
        done, _ = await asyncio.wait({sweep}, timeout=2)
        assert sweep in done, "discovery stalled readable tracked completion"
        sweep.result()
        assert completions == ["A"]
        assert cancellation_seen.is_set() and not release.is_set()
        assert any(not task.done() for task in tasks)
        assert service.health()["attempt_errors"] >= 1
    finally:
        release.set()
        sweep.cancel()
        await asyncio.gather(sweep, return_exceptions=True)
        await asyncio.gather(*tasks, return_exceptions=True)


def test_runtime_failure_fence_survives_resume_and_closing_admission():
    import pytest
    from backend.application.script_authoring.approved_speech import ApprovedSpeech, SpeechRejected

    speech = ApprovedSpeech(None, lambda: None)
    speech.set_runtime_failed("A", True)
    speech.block("A", None)  # Resume cannot erase independent fatal fence.
    with pytest.raises(SpeechRejected, match="runtime_failed"):
        speech.check_start("A")
    # Director/closing speech bypasses check_start; dispatch checks the fatal fence too.
    candidate = SimpleNamespace(envelope=SimpleNamespace(session_id="A"), epoch=0)
    with pytest.raises(SpeechRejected, match="runtime_failed"):
        speech.check_live(candidate, lambda: True)


def test_classification_uses_typed_faults_and_never_provider_text():
    from backend.application.runtime_failures import failure_class
    from backend.application.script_authoring.generation.batch import TransportError, ContentFailure
    from backend.application.publishing.datastream import AvatarStreamError

    assert (
        failure_class(TransportError("safety_failed media_failed token=hidden")) == "runtime_error"
    )
    assert failure_class(ContentFailure("hidden")) == "safety_failed"
    assert failure_class(AvatarStreamError("hidden")) == "media_failed"
    wrapped = RuntimeError("hidden")
    wrapped.__cause__ = AvatarStreamError("hidden")
    assert failure_class(wrapped) == "media_failed"


async def test_real_terminal_completion_never_logs_provider_error_text(caplog):
    from dataclasses import replace

    d, service, owners, blocked, cancelled, invalidated = await fixture()
    service.settings = replace(service.settings, attempt_timeout=1)
    d.coordinator.has = lambda sid: False

    def stop(sid):
        raise RuntimeError("PROVIDER_PRIVATE_TOKEN_SENTINEL")

    d.backend.stop = stop
    service.fail("A", RuntimeError(), "initial")
    await service.sweep()
    assert "PROVIDER_PRIVATE_TOKEN_SENTINEL" not in caplog.text
    assert "RuntimeError" in caplog.text
    assert "A" in service.completion.tracked


async def test_expected_content_expiry_during_dispatch_does_not_fail_healthy_execution():
    from backend.application.director.coordinator import DirectorCoordinator
    from backend.application.director.decision import Decision
    from backend.application.script_authoring.approved_speech import SpeechRejected

    owner = SimpleNamespace(
        generation_token="initial",
        director=SimpleNamespace(cfg=SimpleNamespace(transient_retry_count=0)),
    )
    runtime = SimpleNamespace(
        _sessions={"A": owner}, prompt_layers=lambda *args: {}, has=lambda sid: True
    )
    coordinator = DirectorCoordinator(runtime, None, None, SimpleNamespace())
    coordinator._stats["A"] = SimpleNamespace(skips=0)
    calls, failures = [], []

    async def revalidate(*args, **kwargs):
        calls.append(True)
        if len(calls) == 2:
            raise SpeechRejected("expired_facts")

    coordinator.approved_speech = SimpleNamespace(
        revalidate=revalidate,
        blocked=lambda sid: None,
        guarded_tts=lambda *a, **k: None,
        guarded_audio=lambda *a, **k: None,
    )
    coordinator.runtime_failures = SimpleNamespace(fail=lambda *a: failures.append(a))
    speech = SimpleNamespace(text="approved", evidence=lambda: {})
    decision = Decision(
        "speak_hook", prepared_script="approved", approved_speech=speech, revision_token="initial"
    )
    assert await coordinator._maybe_speak("A", decision)
    assert decision.is_cancelled and not failures
    assert len(calls) == 2
