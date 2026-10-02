"""P0-FB-017 usage evidence: ids, derivation, signer, sender, settings, speech isolation.

Database-backed behavior (sequence, commit gap, delivery order) is in
tests/integration/test_usage_evidence_pg.py.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request

from backend.api.v1.execution import record_execution_evidence, request_execution_command
from backend.application import execution_contract as ec
from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.execution_contract import (
    CommandOutcome,
    Evidence,
    ExecutionIdentity,
    ExecutionState,
    HoldState,
    MediaReadiness,
)
from backend.application.usage_evidence import (
    UsageEvidence,
    UsageEvidenceSettings,
    UsageEvidenceUnavailable,
    UsageSender,
    envelope,
)
from backend.application.usage_evidence.envelope import Draft, Gates
from backend.application.usage_evidence.outbox import Staged
from backend.application.usage_evidence.sender import sign, signed_headers
from backend.application.usage_evidence.settings import CogsBuffer, CogsSample

TENANT = "11111111-2222-4333-8444-555555555555"
IDENT = dict(tenant_id=TENANT, business_session_id="biz", runtime_session_id="rt", generation="g1")
IDENTITY = ExecutionIdentity(**IDENT)
NOW = datetime(2026, 10, 2, 3, 4, 5, 678901, tzinfo=timezone.utc)
FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "usage_evidence_golden_v1.json").read_text("utf-8")
)
ALL_GATES = Gates(first_broadcast=True, media_health=True, hold=True)


def state(**changes: Any) -> ExecutionState:
    return ExecutionState(**IDENT, **changes)


def ev(sequence: int, kind: str, phase: str, **changes: Any) -> Evidence:
    return Evidence(**IDENT, sequence=sequence, kind=kind, phase=phase, occurred_at=NOW, **changes)


def lv(*values: str) -> bytes:
    return b"".join(f"{len(v.encode())}:{v}".encode() for v in values)


# -- ids and the golden fixture ---------------------------------------------------------


def test_interval_and_event_ids_follow_the_contract_formula_independently():
    interval = envelope.interval_id(IDENTITY, "phase:selling", "")
    expected = (
        "ui:" + hashlib.sha256(lv(TENANT, "biz", "rt", "g1", "phase:selling", "")).hexdigest()
    )
    assert interval == expected
    assert envelope.event_id(IDENTITY, "phase_changed", interval) == (
        "ue:" + hashlib.sha256(lv(TENANT, "biz", "rt", "g1", "phase_changed", interval)).hexdigest()
    )


def test_event_id_ignores_producer_send_time_and_usage_sequence():
    draft = Draft("terminal", "terminal", "point", "", "ended", NOW, 9, 9, "normal_end")
    interval = envelope.interval_id(IDENTITY, "terminal", "")
    a = envelope.build_body(IDENTITY, draft, interval=interval, usage_sequence=1, producer_id="a")
    b = envelope.build_body(IDENTITY, draft, interval=interval, usage_sequence=7, producer_id="b")
    assert a[0] == b[0] and a[1] != b[1]


def test_the_producer_reproduces_the_golden_fixture_byte_for_byte():
    identity = ExecutionIdentity(**json.loads(FIXTURE["body"])["payload"]["identity"])
    draft = Draft("terminal", "terminal", "point", "", "ended", NOW, 9, 9, "normal_end")
    interval = envelope.interval_id(identity, "terminal", "")
    eid, body = envelope.build_body(
        identity, draft, interval=interval, usage_sequence=3, producer_id="runtime"
    )
    assert body.decode() == FIXTURE["body"]
    assert eid == FIXTURE["event_id"]
    assert hashlib.sha256(body).hexdigest() == FIXTURE["body_sha256"]
    assert sign(FIXTURE["secret"], FIXTURE["timestamp"], body) == FIXTURE["signature"]


def test_the_body_timestamp_is_the_fact_time_never_the_send_time():
    doc = json.loads(FIXTURE["body"])
    assert doc["timestamp"] == doc["payload"]["occurred_at"] == "2026-10-02T03:04:05.678901Z"


# -- receiver-equivalent verification (mirrors ai_hmac.go / ai_webhook_receiver.go) ---------


def receiver_accepts(secret, headers, body, *, now, tolerance=300, seen=None):
    """Return (status, reason). Same HMAC, timestamp window, prefix and 409 rules as the API."""
    ts, sig = headers["X-AI-Timestamp"], headers["X-AI-Signature"]
    for prefix in ("sha256=", "hmac_sha256="):
        if sig.startswith(prefix):
            sig = sig[len(prefix) :]
    want = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(want, sig.lower()) or abs(now - int(ts)) > tolerance:
        return 401, "unauthorized"
    doc = json.loads(body)
    if headers["X-AI-Event-Id"] != doc["event_id"]:
        return 400, "invalid"
    digest = hashlib.sha256(body).hexdigest()
    prior = seen.get(doc["event_id"]) if seen is not None else None
    if prior is not None and prior != digest:
        return 409, "conflict"
    if seen is not None:
        seen[doc["event_id"]] = digest
    return 202, "duplicate" if prior else "accepted"


def fixture_headers(now=None):
    body = FIXTURE["body"].encode()
    return body, signed_headers(FIXTURE["secret"], FIXTURE["event_id"], body, now or 1790910245)


def test_the_signer_output_verifies_under_the_receiver_rules():
    body, headers = fixture_headers()
    assert receiver_accepts(FIXTURE["secret"], headers, body, now=1790910245)[0] == 202


def test_a_wrong_secret_is_401_and_the_sha256_prefix_is_tolerated():
    body, headers = fixture_headers()
    assert receiver_accepts("wrong", headers, body, now=1790910245)[0] == 401
    prefixed = headers | {"X-AI-Signature": "sha256=" + headers["X-AI-Signature"]}
    assert receiver_accepts(FIXTURE["secret"], prefixed, body, now=1790910245)[0] == 202


def test_a_stale_timestamp_fails_and_a_fresh_resign_over_the_same_bytes_passes():
    body, headers = fixture_headers()
    assert receiver_accepts(FIXTURE["secret"], headers, body, now=1790910245 + 3600)[0] == 401
    fresh = signed_headers(FIXTURE["secret"], FIXTURE["event_id"], body, 1790910245 + 3600)
    assert receiver_accepts(FIXTURE["secret"], fresh, body, now=1790910245 + 3600)[0] == 202


def test_any_tampered_body_byte_breaks_the_signature():
    body, headers = fixture_headers()
    tampered = body.replace(b"normal_end", b"normal_enD")
    assert receiver_accepts(FIXTURE["secret"], headers, tampered, now=1790910245)[0] == 401


def test_replay_is_a_duplicate_and_same_event_id_with_new_bytes_is_409():
    body, headers = fixture_headers()
    seen: dict[str, str] = {}
    assert receiver_accepts(FIXTURE["secret"], headers, body, now=1790910245, seen=seen) == (
        202,
        "accepted",
    )
    assert receiver_accepts(FIXTURE["secret"], headers, body, now=1790910245, seen=seen) == (
        202,
        "duplicate",
    )
    changed = body.replace(b"normal_end", b"normal_enD")
    resigned = signed_headers(FIXTURE["secret"], FIXTURE["event_id"], changed, 1790910245)
    assert (
        receiver_accepts(FIXTURE["secret"], resigned, changed, now=1790910245, seen=seen)[0] == 409
    )


# -- derivation: applied facts only ---------------------------------------------------


def kinds(drafts):
    return [(d.kind, d.interval_kind, d.boundary) for d in drafts]


def test_phase_changed_closing_is_a_start_row_and_a_repeat_emits_nothing():
    prior = state(phase="selling", runtime_ready=True, sequence=4)
    e = ev(5, "phase_changed", "closing")
    updated = ec.apply_evidence(prior, e)
    rows = envelope.derive_from_evidence(prior, updated, e, Gates())
    assert kinds(rows) == [("phase_changed", "phase:closing", "start")]
    assert rows[0].execution_sequence == 5 and rows[0].opening_ref == ""
    same = ev(6, "phase_changed", "closing")
    assert (
        envelope.derive_from_evidence(updated, ec.apply_evidence(updated, same), same, Gates())
        == []
    )


def test_terminal_evidence_emits_one_terminal_point_row_with_its_reason():
    prior = state(phase="ending", runtime_ready=True, sequence=8)
    e = ev(9, "terminal", "ended", reason_code="normal_end")
    rows = envelope.derive_from_evidence(prior, ec.apply_evidence(prior, e), e, Gates())
    assert kinds(rows) == [("terminal", "terminal", "point")]
    assert rows[0].reason_code == "normal_end"


def test_health_false_starts_unusable_true_ends_it_and_same_value_emits_nothing():
    base = state(phase="warming", runtime_ready=True, sequence=5)
    down = ev(6, "health", "warming", healthy=False)
    after_down = ec.apply_evidence(base, down)
    start = envelope.derive_from_evidence(base, after_down, down, Gates())
    assert kinds(start) == [("unusable_started", "unusable", "start")]
    assert start[0].opening_ref == "6"
    again = ev(7, "health", "warming", healthy=False)
    assert (
        envelope.derive_from_evidence(
            after_down, ec.apply_evidence(after_down, again), again, Gates()
        )
        == []
    )
    up = ev(8, "health", "warming", healthy=True)
    end = envelope.derive_from_evidence(after_down, ec.apply_evidence(after_down, up), up, Gates())
    assert kinds(end) == [("unusable_ended", "unusable", "end")]
    assert end[0].opening_ref is None  # resolved from the stored start, never invented
    healthy_first = envelope.derive_from_evidence(
        base,
        ec.apply_evidence(base, ev(6, "health", "warming", healthy=True)),
        ev(6, "health", "warming", healthy=True),
        Gates(),
    )
    assert healthy_first == []


def first_broadcast_case():
    readiness = MediaReadiness(
        readiness_id="r1", destination_id="d", source_id="s", media_ready=True, platform_ready=True
    )
    prior = state(
        phase="ready",
        runtime_ready=True,
        sequence=3,
        media_readiness=readiness,
        start_command_id="start:x",
        opening_turn_id="open1",
    )
    e = ev(
        4,
        "first_ai_broadcast",
        "warming",
        media_readiness=readiness,
        opening_turn_id="open1",
        media_utterance_id="u1",
        media_evidence_id="m1",
    )
    return prior, ec.apply_evidence(prior, e), e


def test_gated_kinds_create_no_row_with_the_gates_off():
    prior, updated, e = first_broadcast_case()
    assert envelope.derive_from_evidence(prior, updated, e, Gates()) == []
    base = state(phase="warming", runtime_ready=True, sequence=5)
    media_down = ev(6, "health", "warming", healthy=False, reason_code="media_egress_down")
    assert (
        envelope.derive_from_evidence(
            base, ec.apply_evidence(base, media_down), media_down, Gates()
        )
        == []
    )
    hold = CommandOutcome(
        **IDENT,
        command_id="c1",
        command="hold",
        actor_id="a",
        requested_at=NOW,
        status="applied",
        result_at=NOW,
        sequence=6,
    )
    held = state(
        phase="selling",
        runtime_ready=True,
        sequence=6,
        hold=HoldState(held=True, hold_command_id="c1"),
    )
    assert (
        envelope.derive_from_command(
            state(phase="selling", runtime_ready=True, sequence=5), held, hold, Gates()
        )
        == []
    )


def test_gated_kinds_match_the_contract_kinds_table_with_the_gates_on():
    prior, updated, e = first_broadcast_case()
    rows = envelope.derive_from_evidence(prior, updated, e, ALL_GATES)
    assert kinds(rows) == [("first_ai_broadcast", "ai_live", "start")]
    assert rows[0].media == {
        "opening_turn_id": "open1",
        "media_utterance_id": "u1",
        "media_evidence_id": "m1",
        "readiness_id": "r1",
    }
    base = state(phase="warming", runtime_ready=True, sequence=5)
    media_down = ev(6, "health", "warming", healthy=False, reason_code="media_egress_down")
    media = envelope.derive_from_evidence(
        base, ec.apply_evidence(base, media_down), media_down, ALL_GATES
    )
    assert media[0].health_source == "media"
    pre = state(phase="selling", runtime_ready=True, sequence=5)
    started = ec.apply_rescue(pre, "hold", "c1", NOW)
    hold = CommandOutcome(
        **IDENT,
        command_id="c1",
        command="hold",
        actor_id="a",
        requested_at=NOW,
        status="applied",
        result_at=NOW,
        sequence=started.sequence,
    )
    rows = envelope.derive_from_command(pre, started, hold, ALL_GATES)
    assert kinds(rows) == [("hold_started", "hold", "start")]
    assert rows[0].opening_ref == "c1" and rows[0].command_id == "c1"
    resume = hold.model_copy(update={"command": "resume", "command_id": "c2"})
    resumed = ec.apply_rescue(started, "resume", "c2", NOW)
    end = envelope.derive_from_command(started, resumed, resume, ALL_GATES)
    assert kinds(end) == [("hold_ended", "hold", "end")]
    assert end[0].opening_ref == "c1"  # the end repeats its start's interval


def test_a_rejected_or_accepted_only_command_emits_nothing():
    pre = state(phase="selling", runtime_ready=True, sequence=5)
    for status in ("rejected", "accepted"):
        out = CommandOutcome(
            **IDENT,
            command_id="c1",
            command="end",
            actor_id="a",
            requested_at=NOW,
            status=status,
            result_at=NOW,
        )
        assert envelope.derive_from_command(pre, pre, out, ALL_GATES) == []


def test_an_applied_end_reports_the_closing_phase_and_emergency_end_the_ending_phase():
    pre = state(phase="selling", runtime_ready=True, sequence=5)
    for command, phase in (("end", "phase:closing"), ("emergency_end", "phase:ending")):
        updated = ec.apply_rescue(pre, command, "c9", NOW)
        out = CommandOutcome(
            **IDENT,
            command_id="c9",
            command=command,
            actor_id="a",
            requested_at=NOW,
            status="applied",
            result_at=NOW,
            sequence=updated.sequence,
        )
        rows = envelope.derive_from_command(pre, updated, out, Gates())
        assert [(r.kind, r.interval_kind) for r in rows] == [("phase_changed", phase)]


def test_vendor_cogs_uses_ai_usage_reported_with_no_interval_and_lifecycle_never_does():
    _, body = envelope.build_cogs_body(
        IDENTITY,
        sample_id="s1",
        occurred_at=NOW,
        model_id="m",
        input_tokens=3,
        output_tokens=4,
        producer_id="runtime",
    )
    doc = json.loads(body)
    assert doc["event_type"] == "ai.usage.reported" and "interval" not in doc["payload"]
    assert json.loads(FIXTURE["body"])["event_type"] == "ai.execution.usage_evidence"


def test_invalid_identity_is_rejected_before_it_can_become_a_poison_row():
    with pytest.raises(envelope.InvalidIdentity):
        envelope.validate_identity(ExecutionIdentity(**(IDENT | {"tenant_id": "tenant"})))
    with pytest.raises(envelope.InvalidIdentity):
        envelope.validate_identity(ExecutionIdentity(**(IDENT | {"generation": "g" * 256})))


def test_no_runtime_code_computes_credits_prices_balances_or_billable_durations():
    forbidden = ("credit", "price", "balance", "billable", "rounding", "grace")
    package = Path(envelope.__file__).parent
    hits = []
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text("utf-8"))
        docstrings = {
            id(n.body[0].value)
            for n in ast.walk(tree)
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and n.body
            and isinstance(n.body[0], ast.Expr)
            and isinstance(n.body[0].value, ast.Constant)
        }
        for n in ast.walk(tree):
            text = None
            if isinstance(n, ast.Name):
                text = n.id
            elif isinstance(n, ast.Attribute):
                text = n.attr
            elif isinstance(n, ast.arg):
                text = n.arg
            elif (
                isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings
            ):
                text = n.value
            if text and any(word in text.lower() for word in forbidden):
                hits.append((path.name, text))
    assert hits == []


# -- settings: default off, fail closed -------------------------------------------------

URL = "https://api.example.test/webhooks/ai/events"


def settings(**env: str) -> UsageEvidenceSettings:
    return UsageEvidenceSettings.from_env({f"USAGE_EVIDENCE_{k}": v for k, v in env.items()})


def test_default_is_off_and_advertises_only_the_base_capability_when_configured():
    assert not settings().configured
    assert settings().capabilities() == ("usage.evidence.v1",)


@pytest.mark.parametrize(
    "env",
    [
        dict(ENABLED="1", URL=URL),  # no secret
        dict(ENABLED="1", SECRET="s"),  # no URL
        dict(URL=URL, SECRET="s"),  # not enabled
        dict(ENABLED="1", SECRET="s", URL=URL + "/"),  # trailing slash: the API router 404s
        dict(ENABLED="1", SECRET="s", URL="https://api.example.test/webhooks/ai"),
        dict(ENABLED="1", SECRET="s", URL="http://api.example.test/webhooks/ai/events"),
        dict(ENABLED="1", SECRET="s", URL="https://u:p@api.example.test/webhooks/ai/events"),
    ],
)
def test_incomplete_or_unsafe_configuration_is_not_configured(env):
    assert not settings(**env).configured


def test_exact_https_or_loopback_http_url_with_secret_is_configured():
    assert settings(ENABLED="1", SECRET="s", URL=URL).configured
    assert settings(ENABLED="1", SECRET="s", URL="http://127.0.0.1:9/webhooks/ai/events").configured


def test_sub_capabilities_follow_their_gates_only():
    caps = settings(ENABLED="1", GATE_HOLD="1", GATE_MEDIA_HEALTH="1").capabilities()
    assert caps == ("usage.evidence.v1", "usage.evidence.media_health", "usage.evidence.hold")


def test_capability_is_computed_and_absent_until_set():
    assert "usage.evidence.v1" not in ec.available_capabilities()
    ec.set_usage_evidence_capabilities(("usage.evidence.v1",))
    try:
        assert "usage.evidence.v1" in ec.available_capabilities(rescue=True)
    finally:
        ec.set_usage_evidence_capabilities(())
    assert "usage.evidence.v1" not in ec.Capabilities().available


# -- sender classification (fake outbox, no network) --------------------------------------


class FakeOutbox:
    def __init__(self, rows):
        self.rows, self.finished = rows, []

    async def claim(self, limit, lease):
        rows, self.rows = self.rows, []
        return rows

    async def finish(self, eid, token, status, **kw):
        self.finished.append((eid, status, kw))
        return True


def row(attempts=1):
    return dict(
        event_id=FIXTURE["event_id"],
        body=FIXTURE["body"].encode(),
        attempts=attempts,
        usage_sequence=3,
        lease_token="t",
    )


async def run_once(post, *, attempts=1, now=1790910245.0):
    outbox = FakeOutbox([row(attempts)])
    sender = UsageSender(
        outbox,  # type: ignore[arg-type]
        settings(ENABLED="1", SECRET=FIXTURE["secret"], URL=URL),
        post=post,
        clock=lambda: now,
        rng=lambda: 0.5,
    )
    await sender.deliver_due()
    return outbox.finished[0], sender


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (202, "delivered"),
        (409, "conflict"),
        (400, "rejected"),
        (413, "rejected"),
        (401, "ready"),
        (503, "ready"),
        (500, "ready"),
        (429, "ready"),
        (200, "ready"),
    ],
)
async def test_response_classification(status, outcome):
    async def post(url, body, headers):
        return status

    (_, got, _), _ = await run_once(post)
    assert got == outcome


async def test_transport_failures_retry_with_a_capped_backoff_and_never_drop():
    async def post(url, body, headers):
        raise ConnectionRefusedError("down")

    (_, got, kw), _ = await run_once(post, attempts=30)
    assert got == "ready" and kw["error"].startswith("transport_")
    assert 0 < kw["retry_in"] <= 300  # cap, with jitter


async def test_a_permanent_conflict_is_counted_and_never_resent_with_new_bytes():
    sent = []

    async def post(url, body, headers):
        sent.append(body)
        return 409

    _, sender = await run_once(post)
    assert sender.permanent_failures == 1 and sent == [FIXTURE["body"].encode()]


async def test_retries_resend_identical_bytes_changing_only_timestamp_and_signature():
    seen = []

    async def post(url, body, headers):
        seen.append((url, body, dict(headers)))
        return 503

    await run_once(post, now=1790910245.0)
    await run_once(post, now=1790910545.0)
    (u1, b1, h1), (u2, b2, h2) = seen
    assert u1 == u2 == URL and b1 == b2 == FIXTURE["body"].encode()
    changed = {k for k in h1 if h1[k] != h2[k]}
    assert changed == {"X-AI-Timestamp", "X-AI-Signature"}
    assert h1["X-AI-Event-Id"] == FIXTURE["event_id"]


# -- speech path isolation ------------------------------------------------------------------


def test_record_cogs_is_synchronous_and_never_touches_the_event_loop():
    assert not inspect.iscoroutinefunction(UsageEvidence.record_cogs)


def test_cogs_buffer_overflow_drops_the_oldest_and_counts_without_blocking():
    buffer = CogsBuffer(2)
    for i in range(5):
        buffer.append(CogsSample(IDENTITY, f"s{i}", NOW, "m", i, i))
    assert buffer.dropped == 3
    assert [s.sample_id for s in buffer.drain(10)] == ["s3", "s4"]


async def test_a_hung_webhook_does_not_delay_the_event_loop_or_cogs_recording():
    release = asyncio.Event()

    async def hang(url, body, headers):
        await release.wait()
        return 202

    outbox = FakeOutbox([row()])
    sender = UsageSender(
        outbox,
        settings(ENABLED="1", SECRET="s", URL=URL),
        post=hang,  # type: ignore[arg-type]
    )
    task = asyncio.create_task(sender.deliver_due())
    await asyncio.sleep(0)
    service = UsageEvidence(SimpleNamespace(), settings(ENABLED="1", COGS_BUFFER="4"))  # type: ignore[arg-type]
    started = asyncio.get_running_loop().time()
    for _ in range(100):
        service.record_cogs(IDENTITY, model_id="m", input_tokens=1, output_tokens=1)
    await asyncio.sleep(0)
    assert asyncio.get_running_loop().time() - started < 0.5 and not task.done()
    release.set()
    await task


def test_the_speech_and_decision_modules_never_import_the_usage_evidence_package():
    root = Path(envelope.__file__).parents[2]
    for rel in ("director/coordinator.py", "director/decision.py", "director/session_context.py"):
        assert "usage_evidence" not in (root / "application" / rel).read_text("utf-8")


# -- the control-plane endpoints: fail closed, rejected emits nothing, save fence ----------


class FakeUsage:
    def __init__(self, fail=False):
        self.fail, self.staged, self.committed, self.aborted = fail, [], [], []

    async def stage_evidence(self, prior, updated, e):
        if self.fail:
            raise UsageEvidenceUnavailable("down")
        self.staged.append(e.sequence)
        return [Staged(f"e{e.sequence}", "staged", e.sequence, True)]

    async def stage_command(self, prior, updated, outcome):
        return []

    @staticmethod
    def stamp(meta, staged):
        UsageEvidence.stamp(meta, staged)

    async def commit(self, staged):
        self.committed.extend(s.event_id for s in staged)

    async def abort(self, staged):
        self.aborted.extend(s.event_id for s in staged)


async def make_request(usage=None, store=None):
    store = store or InMemorySessionStore()
    await store.set("rt", {"execution_contract": state().model_dump(mode="json")})
    container = SimpleNamespace(store=store)
    if usage is not None:
        container.usage_evidence = usage
    return Request(
        {"type": "http", "app": SimpleNamespace(state=SimpleNamespace(container=container))}
    ), store


async def test_applied_evidence_is_staged_before_save_and_committed_after():
    usage = FakeUsage()
    request, _ = await make_request(usage)
    await record_execution_evidence("rt", ev(1, "runtime_ready", "ready"), request, None)
    assert usage.staged == [1] and usage.committed == ["e1"] and usage.aborted == []


async def test_rejected_evidence_emits_nothing():
    usage = FakeUsage()
    request, _ = await make_request(usage)
    with pytest.raises(HTTPException):
        await record_execution_evidence("rt", ev(1, "phase_changed", "selling"), request, None)
    assert usage.staged == [] and usage.committed == []


async def test_an_ambiguous_save_failure_keeps_the_staged_rows_for_the_sweeper():
    """Redis may have committed although the reply was lost: never delete the row."""
    usage = FakeUsage()

    class LostReply(InMemorySessionStore):
        async def set(self, key, value):
            await super().set(key, value)  # the write LANDED ...
            if key == "rt" and value.get("execution_contract", {}).get("sequence") == 1:
                raise RuntimeError("reply lost")  # ... but the caller sees an error

    request, store = await make_request(usage, LostReply())
    with pytest.raises(RuntimeError):
        await record_execution_evidence("rt", ev(1, "runtime_ready", "ready"), request, None)
    assert usage.aborted == [] and usage.committed == []
    meta = await store.get("rt")
    assert meta["execution_contract"]["sequence"] == 1
    assert meta["usage_evidence_committed"] == ["e1"]  # the proof the sweeper reads


async def test_a_definite_fence_refusal_aborts_the_staged_rows():
    usage = FakeUsage()

    class Refusing(InMemorySessionStore):
        async def set(self, key, value):
            if value.get("execution_contract", {}).get("sequence") == 1:
                raise HTTPException(status_code=503, detail={"code": "session_busy"})
            await super().set(key, value)

    request, store = await make_request(usage, Refusing())
    with pytest.raises(HTTPException):
        await record_execution_evidence("rt", ev(1, "runtime_ready", "ready"), request, None)
    assert usage.aborted == ["e1"] and usage.committed == []


async def test_an_unavailable_outbox_answers_503_and_the_fact_is_not_applied():
    request, store = await make_request(FakeUsage(fail=True))
    with pytest.raises(HTTPException) as caught:
        await record_execution_evidence("rt", ev(1, "runtime_ready", "ready"), request, None)
    assert caught.value.status_code == 503
    assert caught.value.detail == {"code": "usage_evidence_unavailable"}
    assert (await store.get("rt"))["execution_contract"]["sequence"] == 0


async def test_without_the_service_the_evidence_request_still_succeeds_and_writes_nothing():
    request, _ = await make_request(None)
    out = await record_execution_evidence("rt", ev(1, "runtime_ready", "ready"), request, None)
    assert out["state"]["phase"] == "ready"


async def test_a_rejected_command_emits_nothing_through_the_endpoint():
    usage = FakeUsage()
    request, _ = await make_request(usage)
    from backend.application.execution_contract import CommandRequest

    req = CommandRequest(**IDENT, command_id="c1", command="end", actor_id="a", requested_at=NOW)
    out = await request_execution_command("rt", req, request, None)
    assert out["outcome"]["status"] == "rejected" and usage.committed == []


# -- Codex review fixes -----------------------------------------------------------------------


def test_the_url_path_must_equal_the_exact_receiver_path_not_merely_end_with_it():
    base = "https://api.example.test"
    assert not settings(ENABLED="1", SECRET="s", URL=base + "/wrong/webhooks/ai/events").configured
    assert not settings(ENABLED="1", SECRET="s", URL=base + "/x/webhooks/ai/events/").configured
    assert not settings(ENABLED="1", SECRET="s", URL=base + "/webhooks/ai/events?a=1").configured
    assert not settings(ENABLED="1", SECRET="s", URL=base + "/webhooks/ai/events#f").configured
    assert settings(ENABLED="1", SECRET="s", URL=base + "/webhooks/ai/events").configured


def test_no_credential_appears_in_repr_or_str_of_settings_sender_or_outbox():
    secret = "super-secret-signing-key-0123456789"
    cfg = settings(ENABLED="1", SECRET=secret, URL=URL)
    assert cfg.secret == secret  # still usable ...
    outbox = SimpleNamespace(dsn="postgresql://u:pw-hunter2@h/db")
    sender = UsageSender(outbox, cfg, post=lambda *a: None)  # type: ignore[arg-type]
    facade = UsageEvidence(outbox, cfg)  # type: ignore[arg-type]
    for obj in (cfg, sender, facade):
        assert secret not in repr(obj) and secret not in str(obj)
    for dc in (UsageEvidenceSettings, CogsSample):
        import dataclasses

        for f in dataclasses.fields(dc):
            assert not (f.name in ("secret", "token", "password") and f.repr)


class InvalidOutbox:
    async def stage(self, identity, drafts):
        envelope.validate_identity(identity)
        raise AssertionError("unreachable")


async def test_an_undeliverable_identity_fails_closed_422_and_state_does_not_advance():
    cfg = settings(ENABLED="1", SECRET="s", URL=URL)
    usage = UsageEvidence(InvalidOutbox(), cfg)  # type: ignore[arg-type]
    store = InMemorySessionStore()
    bad = IDENT | {"tenant_id": "not-a-uuid"}
    await store.set("rt", {"execution_contract": ExecutionState(**bad).model_dump(mode="json")})
    request = Request(
        {
            "type": "http",
            "app": SimpleNamespace(
                state=SimpleNamespace(container=SimpleNamespace(store=store, usage_evidence=usage))
            ),
        }
    )
    first = ev(1, "runtime_ready", "ready").model_copy(update=bad)
    await record_execution_evidence("rt", first, request, None)  # derives no row: allowed
    evidence = ev(2, "health", "ready", healthy=False, reason_code="runtime_down").model_copy(
        update=bad
    )
    with pytest.raises(HTTPException) as caught:
        await record_execution_evidence("rt", evidence, request, None)
    assert caught.value.status_code == 422
    assert caught.value.detail == {"code": "usage_evidence_invalid_identity"}
    assert (await store.get("rt"))["execution_contract"]["sequence"] == 1  # never advanced


def test_the_committed_fact_stamp_is_bounded_most_recent_and_deduplicated():
    meta: dict[str, Any] = {}
    for i in range(300):
        UsageEvidence.stamp(meta, [Staged(f"e{i}", "staged", None, True)])
    UsageEvidence.stamp(meta, [Staged("e299", "staged", None, False)])
    assert len(meta["usage_evidence_committed"]) == 256
    assert meta["usage_evidence_committed"][-1] == "e299"
    assert meta["usage_evidence_committed"].count("e299") == 1
    assert "e0" not in meta["usage_evidence_committed"]
