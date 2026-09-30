"""P0-FB-019 (disabled slice): hot state -> terminal record, persist-before-delete, outbox delivery."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from backend.api.v1.sessions import _stop_cancelled_session
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import (
    Cleanup,
    ExecutionIdentity,
    TerminalRecord,
    validate_terminal_record,
)
from backend.application.publishing.legacy import LiveKitPublisherRegistry
from backend.application.terminal_outcomes import (
    ATTEMPTS_KEY,
    CLEANUP_MAX_ATTEMPTS,
    PENDING_FLAG,
    PersistResult,
    TerminalOutbox,
    TerminalOutcomes,
    TerminalSettings,
    backoff_seconds,
    build_terminal_record,
)

NOW = datetime(2026, 9, 30, 1, 2, 3, 456789, tzinfo=timezone.utc)
IDENTITY = dict(
    tenant_id="tenant-1",
    business_session_id="business-1",
    runtime_session_id="rt-1",
    generation="g1",
)
OK = Cleanup(status="succeeded", attempts=1)


def build(meta, cleanup=OK):
    record = build_terminal_record(meta, now=NOW, cleanup=cleanup)
    assert record is not None
    return record


def hot(phase="selling", **extra):
    meta = {
        "status": "active",
        "execution_contract": {
            **IDENTITY,
            "sequence": 7,
            "phase": phase,
            "runtime_ready": True,
            "first_ai_broadcast": True,
            "first_playable_evidence_id": "media-1",
        },
        "execution_command_outcomes": {},
    }
    meta.update(extra)
    return meta


def applied(command, command_id):
    return {
        "command_id": command_id,
        "command": command,
        "actor_id": "owner-1",
        "requested_at": "2026-09-30T01:00:00Z",
        "status": "applied",
        "result_at": "2026-09-30T01:00:01Z",
        **IDENTITY,
    }


def test_legacy_session_has_no_terminal_record():
    assert build_terminal_record({"status": "active"}, now=NOW, cleanup=OK) is None
    assert build_terminal_record(None, now=NOW, cleanup=OK) is None


def test_applied_end_is_ended_normal_end_with_command_ref_and_honest_nulls():
    record = build(hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}))
    validate_terminal_record(record)
    assert (record.terminal_phase, record.reason_code, record.business_outcome) == (
        "ended",
        "normal_end",
        "ENDED",
    )
    assert record.command_ref is not None
    assert record.source == "merchant_command" and record.command_ref.command_id == "c1"
    assert record.closing_started_at is None and record.ending_started_at is None
    assert record.stop_requested_at == datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
    assert record.first_ai_broadcast and record.first_playable_evidence_id == "media-1"


def test_applied_emergency_end_wins_over_end_and_is_never_failed():
    record = build(
        hot(
            "ending",
            execution_command_outcomes={
                "c1": applied("end", "c1"),
                "c2": applied("emergency_end", "c2"),
            },
        )
    )
    assert record.command_ref is not None
    assert (record.terminal_phase, record.reason_code, record.command_ref.command_id) == (
        "ended",
        "merchant_emergency_end",
        "c2",
    )


def test_recorded_failed_phase_stays_failed_even_if_an_end_was_applied():
    record = build(
        hot(
            "failed",
            execution_command_outcomes={"c1": applied("end", "c1")},
            execution_failure_class="media_failed",
        )
    )
    assert (record.terminal_phase, record.failure_class, record.business_outcome) == (
        "failed",
        "media_failed",
        "FAILED",
    )


def test_unproven_stop_is_conservatively_failed_not_ended():
    record = build(hot("selling"))
    assert (record.terminal_phase, record.failure_class, record.command_ref) == (
        "failed",
        "control_lost",
        None,
    )


def test_unknown_failure_hint_is_not_trusted():
    assert (
        build(hot("failed", execution_failure_class="'; drop table")).failure_class
        == "runtime_error"
    )


def test_the_record_carries_the_observed_cleanup_not_an_assumed_success():
    failed = Cleanup(status="failed", attempts=3, last_error_class="RuntimeError")
    record = build(hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}), failed)
    assert (record.terminal_phase, record.cleanup.status, record.cleanup.attempts) == (
        "ended",
        "failed",
        3,
    )


class FakePg:
    def __init__(self, fail_with=None, registered=None):
        self.enabled = True
        self.fail_with = fail_with
        self.registered = registered or {}
        self.stored = {}
        self.calls = []
        self.deferred = []

    async def get_terminal_execution(self, session_id):
        return self.registered.get(session_id)

    async def defer_terminal(self, session_id, reason):
        self.deferred.append((session_id, reason))

    async def list_unterminated_executions(self):
        return [
            i
            for i in self.registered.values()
            if (i.tenant_id, i.business_session_id, i.generation) not in self.stored
        ]

    async def persist_terminal(self, record):
        self.calls.append("persist")
        if self.fail_with:
            raise self.fail_with
        key = (
            record.identity.tenant_id,
            record.identity.business_session_id,
            record.identity.generation,
        )
        if key in self.stored:
            return PersistResult(self.stored[key], False, "")
        self.stored[key] = record
        return PersistResult(record, True, "")


async def test_duplicate_stop_returns_the_same_record():
    store, pg = InMemorySessionStore(), FakePg()
    await store.set("s", hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}))
    clock = iter([NOW, NOW.replace(second=59)])
    terminal = TerminalOutcomes(pg, clock=lambda: next(clock))
    first = await terminal.persist_before_delete(store, "s", OK)
    second = await terminal.persist_before_delete(store, "s", OK)
    assert first is not None and second is not None
    assert second.record_hash == first.record_hash and len(pg.stored) == 1


def stop_container(calls, terminal=None, publishers=None):
    class Backend:
        def stop(self, session_id):
            calls.append("backend.stop")
            if session_id not in store._store:
                raise KeyError(session_id)

    class Store(InMemorySessionStore):
        async def delete(self, session_id):
            calls.append("store.delete")
            return await super().delete(session_id)

    store = Store()
    return SimpleNamespace(
        orchestrators={},
        backend=Backend(),
        livekit_publishers=publishers,
        director=None,
        store=store,
        hub=None,
        locks=SimpleNamespace(drop=lambda _sid: None),
        terminal_outcomes=terminal,
    )


async def test_disabled_stop_is_the_legacy_path_with_no_persist():
    calls = []
    d = stop_container(calls)
    await d.store.set("s", hot("ending"))
    d.backend.stop = lambda sid: calls.append("backend.stop")
    del d.terminal_outcomes  # an old container without the attribute
    assert await _stop_cancelled_session(d, "s") == {"ok": True, "stopped": "s"}
    assert calls == ["backend.stop", "store.delete"]
    assert await d.store.get("s") is None


async def test_disabled_stop_still_propagates_a_livekit_failure_unchanged():
    class Boom:
        async def stop(self, session_id):
            raise RuntimeError("livekit down")

    d = stop_container([], publishers=Boom())
    await d.store.set("s", hot("ending"))
    d.backend.stop = lambda sid: None
    with pytest.raises(RuntimeError, match="livekit down"):
        await _stop_cancelled_session(d, "s")


async def test_enabled_stop_persists_before_hot_state_is_deleted():
    calls, pg = [], FakePg()
    d = stop_container(calls)
    await d.store.set("s", hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}))
    d.backend.stop = lambda sid: calls.append("backend.stop")
    original = pg.persist_terminal

    async def recording(record):
        calls.append("terminal.persist")
        return await original(record)

    pg.persist_terminal = recording
    d.terminal_outcomes = TerminalOutcomes(pg, clock=lambda: NOW)
    await _stop_cancelled_session(d, "s")
    assert calls == ["backend.stop", "terminal.persist", "store.delete"]
    assert await d.store.get("s") is None and len(pg.stored) == 1
    (record,) = pg.stored.values()
    assert record.cleanup.status == "succeeded"


async def test_db_unavailable_keeps_hot_state_reports_ending_retry_and_never_claims_ended():
    calls, pg = [], FakePg(fail_with=ConnectionError("db down"))
    d = stop_container(calls)
    await d.store.set("s", hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}))
    d.backend.stop = lambda sid: calls.append("backend.stop")
    d.terminal_outcomes = TerminalOutcomes(pg, clock=lambda: NOW)
    with pytest.raises(HTTPException) as caught:
        await _stop_cancelled_session(d, "s")
    assert caught.value.status_code == 503
    assert caught.value.detail == {
        "code": "terminal_persist_failed",
        "phase": "ending",
        "retry": True,
    }
    assert "store.delete" not in calls
    kept = await d.store.get("s")
    assert kept is not None and kept[PENDING_FLAG] is True

    # Retry after the DB is back: the backend is already stopped (404 on the legacy
    # path), but the pending marker lets the same stop finish persist-then-delete.
    pg.fail_with = None

    def gone(sid):
        calls.append("backend.stop")
        raise KeyError(sid)

    d.backend.stop = gone
    assert await _stop_cancelled_session(d, "s") == {"ok": True, "stopped": "s"}
    assert calls[-2:] == ["backend.stop", "store.delete"] and len(pg.stored) == 1


async def test_unknown_session_is_still_404_without_a_pending_marker():
    calls, pg = [], FakePg()
    d = stop_container(calls)
    d.terminal_outcomes = TerminalOutcomes(pg, clock=lambda: NOW)
    with pytest.raises(HTTPException) as caught:
        await _stop_cancelled_session(d, "missing")
    assert caught.value.status_code == 404 and pg.calls == []


class FailingPublishers:
    def __init__(self, failures):
        self.failures, self.calls = failures, 0

    async def stop(self, session_id):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("livekit disconnect failed")


async def test_a_livekit_cleanup_failure_takes_the_503_ending_retry_path_and_is_then_recorded_failed():
    calls, pg = [], FakePg()
    publishers = FailingPublishers(failures=CLEANUP_MAX_ATTEMPTS)
    d = stop_container(calls, publishers=publishers)
    await d.store.set("s", hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}))
    d.backend.stop = lambda sid: calls.append("backend.stop")
    d.terminal_outcomes = TerminalOutcomes(pg, clock=lambda: NOW)

    for attempt in range(1, CLEANUP_MAX_ATTEMPTS):
        with pytest.raises(HTTPException) as caught:
            await _stop_cancelled_session(d, "s")
        assert caught.value.detail == {
            "code": "terminal_cleanup_retry",
            "phase": "ending",
            "retry": True,
        }
        kept = await d.store.get("s")
        assert kept is not None and kept[ATTEMPTS_KEY] == attempt and kept[PENDING_FLAG] is True
        assert pg.stored == {}  # nothing is claimed while the retry can still succeed
        d.backend.stop = lambda sid: (_ for _ in ()).throw(KeyError(sid))  # already stopped

    # Retries exhausted: ENDED is preserved and the failed cleanup is what is recorded.
    assert await _stop_cancelled_session(d, "s") == {"ok": True, "stopped": "s"}
    (record,) = pg.stored.values()
    assert (record.terminal_phase, record.cleanup.status, record.cleanup.attempts) == (
        "ended",
        "failed",
        3,
    )
    assert record.cleanup.last_error_class == "RuntimeError"


async def test_a_cleanup_failure_that_recovers_records_success_with_the_attempt_count():
    calls, pg = [], FakePg()
    d = stop_container(calls, publishers=FailingPublishers(failures=1))
    await d.store.set("s", hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}))
    d.backend.stop = lambda sid: calls.append("backend.stop")
    d.terminal_outcomes = TerminalOutcomes(pg, clock=lambda: NOW)
    with pytest.raises(HTTPException):
        await _stop_cancelled_session(d, "s")
    d.backend.stop = lambda sid: (_ for _ in ()).throw(KeyError(sid))
    await _stop_cancelled_session(d, "s")
    (record,) = pg.stored.values()
    assert (record.cleanup.status, record.cleanup.attempts) == ("succeeded", 2)


async def test_the_publisher_registry_keeps_its_entry_until_the_stop_succeeded():
    class Publisher:
        def __init__(self):
            self.fail = True

        async def stop(self):
            if self.fail:
                raise RuntimeError("disconnect failed")

    publisher = Publisher()
    registry = LiveKitPublisherRegistry(lambda sid: publisher)
    registry.activate("s")
    from backend.application.publishing.legacy import _RegistryEntry

    registry._entries["s"] = _RegistryEntry(publisher)
    with pytest.raises(RuntimeError):
        await registry.stop("s")
    assert registry.session_ids == ("s",)  # a retry can still reach the publisher
    publisher.fail = False
    await registry.stop("s")
    assert registry.session_ids == ()


def lost_identity():
    return ExecutionIdentity(**IDENTITY)


async def test_missing_hot_state_for_a_registered_execution_is_recorded_failed_runtime_lost():
    calls, pg = [], FakePg(registered={"s": lost_identity()})
    d = stop_container(calls)
    d.backend.stop = lambda sid: calls.append("backend.stop")  # the backend still knows it
    d.terminal_outcomes = TerminalOutcomes(pg, clock=lambda: NOW)
    assert await _stop_cancelled_session(d, "s") == {"ok": True, "stopped": "s"}
    (record,) = pg.stored.values()
    assert (record.terminal_phase, record.failure_class, record.business_outcome) == (
        "failed",
        "runtime_lost",
        "FAILED",
    )
    assert record.evidence_refs.diagnostic_ref == "hot_state_missing"
    assert calls == ["backend.stop", "store.delete"]


async def test_missing_hot_state_with_no_registration_is_an_audited_deferral_never_silent():
    calls, pg = [], FakePg()
    d = stop_container(calls)
    d.backend.stop = lambda sid: calls.append("backend.stop")
    d.terminal_outcomes = TerminalOutcomes(pg, clock=lambda: NOW)
    await _stop_cancelled_session(d, "s")
    assert pg.deferred == [("s", "hot_state_missing_unregistered")] and pg.stored == {}


async def test_shutdown_persists_a_record_for_every_unterminated_execution_before_components_stop():
    store = InMemorySessionStore()
    live = ExecutionIdentity(**IDENTITY)
    gone = ExecutionIdentity(
        **(IDENTITY | {"runtime_session_id": "rt-2", "business_session_id": "business-2"})
    )
    await store.set("rt-1", hot("ending", execution_command_outcomes={"c1": applied("end", "c1")}))
    pg = FakePg(registered={"rt-1": live, "rt-2": gone})
    assert await TerminalOutcomes(pg, clock=lambda: NOW).persist_active_on_shutdown(store) == 2
    by_session = {k[1]: v for k, v in pg.stored.items()}
    assert by_session["business-1"].terminal_phase == "ended"
    assert by_session["business-1"].cleanup.status == "pending"  # teardown has not happened yet
    assert by_session["business-2"].failure_class == "runtime_lost"


async def test_a_shutdown_record_that_cannot_be_stored_leaves_an_audited_deferral():
    pg = FakePg(fail_with=ConnectionError("db down"), registered={"rt-1": lost_identity()})
    assert (
        await TerminalOutcomes(pg, clock=lambda: NOW).persist_active_on_shutdown(
            InMemorySessionStore()
        )
        == 0
    )
    assert pg.deferred == [("rt-1", "shutdown_persist_failed")]


@pytest.mark.parametrize(
    "url,configured",
    [
        ("https://api.example/x", True),
        ("http://127.0.0.1:8006/x", True),
        ("http://localhost/x", True),
        ("http://[::1]:8006/x", True),
        ("http://localhost.attacker.example/x", False),
        ("http://127.0.0.1.evil.example/x", False),
        ("http://127.0.0.1@evil.example/x", False),
        ("http://evil.example/x", False),
        ("https://user:pw@api.example/x", False),
        ("ftp://127.0.0.1/x", False),
        ("https:///no-host", False),
        ("http://127.0.0.1:notaport/x", False),
        ("", False),
    ],
)
def test_callback_url_is_parsed_not_prefix_matched(url, configured):
    env = {
        "TERMINAL_OUTCOMES_ENABLED": "1",
        "TERMINAL_CALLBACK_URL": url,
        "TERMINAL_CALLBACK_SECRET": "s",
    }
    assert TerminalSettings.from_env(env).configured is configured


def test_settings_require_flag_and_secret():
    env = {
        "TERMINAL_OUTCOMES_ENABLED": "1",
        "TERMINAL_CALLBACK_URL": "https://api.example/x",
        "TERMINAL_CALLBACK_SECRET": "s",
    }
    assert TerminalSettings.from_env(env).configured
    assert not TerminalSettings.from_env({}).configured
    assert not TerminalSettings.from_env({**env, "TERMINAL_OUTCOMES_ENABLED": "0"}).configured
    assert not TerminalSettings.from_env({**env, "TERMINAL_CALLBACK_SECRET": ""}).configured


class OutboxPg:
    def __init__(self, rows, accept=True):
        self.rows, self.finished, self.accept = rows, [], accept

    async def claim_terminal_outbox(self, limit, lease):
        rows, self.rows = self.rows, []
        return rows

    async def finish_terminal_outbox(
        self, record_id, record_hash, token, status, *, error=None, retry_in=0.0
    ):
        self.finished.append((record_id, record_hash, token, status, error, retry_in))
        return self.accept


def outbox_with(post, kind="primary", accept=True):
    rows = [
        {
            "terminal_record_id": "tr:0",
            "record_hash": "h0",
            "kind": kind,
            "body": '{"a":1}',
            "attempts": 2,
            "lease_token": "tok-1",
        }
    ]
    pg = OutboxPg(rows, accept)
    settings = TerminalSettings(True, "https://api.example/records", "sekret")
    return TerminalOutbox(pg, settings, post=post, rng=lambda: 0.0), pg


@pytest.mark.parametrize(
    "kind,status,payload,expected",
    [
        ("primary", 201, None, ("delivered", None)),
        ("primary", 200, None, ("delivered", None)),
        (
            "primary",
            409,
            {"data": {"code": "already_terminal"}},
            ("rejected", "http_409_already_terminal"),
        ),
        (
            "primary",
            409,
            {"data": {"code": "stale_generation"}},
            ("rejected", "http_409_stale_generation"),
        ),
        ("primary", 409, {"data": {"code": "x y; drop"}}, ("rejected", "http_409_unknown")),
        ("late_evidence", 409, {"data": {"code": "already_terminal"}}, ("delivered", None)),
        (
            "late_evidence",
            409,
            {"data": {"code": "stale_generation"}},
            ("rejected", "http_409_stale_generation"),
        ),
        ("primary", 422, None, ("rejected", "http_422")),
        ("primary", 401, None, ("pending", "http_401")),
        ("primary", 404, None, ("pending", "http_404")),
        ("primary", 503, None, ("pending", "http_503")),
    ],
)
async def test_outbox_classifies_api_answers(kind, status, payload, expected):
    async def post(url, body, headers):
        return status, payload

    outbox, pg = outbox_with(post, kind)
    assert await outbox.deliver_due() == 1
    (_, _, _, got_status, got_error, retry_in) = pg.finished[0]
    assert (got_status, got_error) == expected
    assert (retry_in > 0) == (got_status == "pending")


async def test_outbox_finishes_with_the_lease_token_it_claimed_and_survives_losing_the_lease():
    async def post(url, body, headers):
        return 201, None

    outbox, pg = outbox_with(post, accept=False)  # the store refuses: the lease expired
    assert await outbox.deliver_due() == 1
    assert pg.finished[0][:4] == ("tr:0", "h0", "tok-1", "delivered")


async def test_outbox_resends_identical_bytes_with_the_secret_header_and_retries_transport_errors():
    seen = []

    async def post(url, body, headers):
        seen.append((url, body, headers["X-Livento-Internal-Secret"]))
        raise TimeoutError("slow")

    outbox, pg = outbox_with(post)
    await outbox.deliver_due()
    assert seen == [("https://api.example/records", b'{"a":1}', "sekret")]
    assert pg.finished[0][3:5] == ("pending", "transport_TimeoutError")


def test_backoff_is_capped_and_jittered():
    assert backoff_seconds(1, rng=lambda: 0.0) == 0.5
    assert backoff_seconds(3, rng=lambda: 1.0) == 4.0
    assert backoff_seconds(40, rng=lambda: 1.0) == 300.0


def test_record_is_a_terminal_record_model():
    assert TerminalRecord.model_validate(build(hot("failed")).model_dump(mode="json"))


class LifespanPg:
    enabled = True

    def __init__(self, fail_schema=False):
        self.fail_schema, self.schema_applied = fail_schema, 0

    async def apply_terminal_schema(self):
        if self.fail_schema:
            raise RuntimeError("no rights")
        self.schema_applied += 1

    async def claim_terminal_outbox(self, limit, lease):
        return []


ENABLED_ENV = {
    "TERMINAL_OUTCOMES_ENABLED": "1",
    "TERMINAL_CALLBACK_URL": "https://api.example/records",
    "TERMINAL_CALLBACK_SECRET": "s",
}


async def start_lifespan_stage(monkeypatch, env, pg):
    from backend.application.execution_contract import Capabilities, TERMINAL_CAPABILITY
    from backend.bootstrap import lifespan

    for name in ENABLED_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    container: Any = SimpleNamespace(pg_store=pg, terminal_outcomes=None, terminal_outbox_task=None)
    await lifespan._start_terminal_outcomes(container)
    advertised = Capabilities().supports(TERMINAL_CAPABILITY)
    await lifespan._stop_terminal_outcomes(container)
    return container, advertised, Capabilities().supports(TERMINAL_CAPABILITY)


async def test_default_environment_wires_nothing_and_never_advertises(monkeypatch):
    pg = LifespanPg()
    container, advertised, _ = await start_lifespan_stage(monkeypatch, {}, pg)
    assert (container.terminal_outcomes, container.terminal_outbox_task, advertised) == (
        None,
        None,
        False,
    )
    assert pg.schema_applied == 0


@pytest.mark.parametrize(
    "env,pg",
    [
        ({**ENABLED_ENV, "TERMINAL_CALLBACK_SECRET": ""}, LifespanPg()),
        ({**ENABLED_ENV, "TERMINAL_CALLBACK_URL": ""}, LifespanPg()),
        (
            {**ENABLED_ENV, "TERMINAL_CALLBACK_URL": "http://localhost.attacker.example/x"},
            LifespanPg(),
        ),
        (ENABLED_ENV, None),
        (ENABLED_ENV, SimpleNamespace(enabled=False)),
        (ENABLED_ENV, LifespanPg(fail_schema=True)),
    ],
    ids=["no-secret", "no-url", "attacker-host", "no-store", "store-disabled", "schema-failed"],
)
async def test_enabled_but_unusable_keeps_the_capability_absent(monkeypatch, env, pg):
    container, advertised, _ = await start_lifespan_stage(monkeypatch, env, pg)
    assert (container.terminal_outcomes, advertised) == (None, False)


async def test_enabled_with_a_durable_store_advertises_then_withdraws_on_shutdown(monkeypatch):
    pg = LifespanPg()
    container, advertised, after_stop = await start_lifespan_stage(monkeypatch, ENABLED_ENV, pg)
    assert container.terminal_outcomes is not None and advertised and not after_stop
    assert pg.schema_applied == 1 and container.terminal_outbox_task.done()


def test_schema_applied_at_every_startup_has_no_terminal_tables():
    from backend.application.db.postgres_store import schema_sql

    assert "terminal_" not in schema_sql()
