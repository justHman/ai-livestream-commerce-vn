"""M7.3 runtime cost evidence (ai.cost.reported): producer, envelope, gates, isolation."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from backend.application.clients.composition import OpenAICompatLLMEngine
from backend.application.execution_contract import ExecutionIdentity
from backend.application.render.orchestrator import StreamOrchestrator
from backend.application.usage_evidence import UsageEvidence, UsageEvidenceSettings, UsageSender
from backend.application.usage_evidence import envelope
from backend.application.usage_evidence.cost import CostMeter, cost_meter
from llm.engines.base import LLMRequest

TENANT = "11111111-2222-4333-8444-555555555555"
IDENTITY = ExecutionIdentity(
    tenant_id=TENANT, business_session_id="biz", runtime_session_id="rt", generation="g1"
)
FORBIDDEN = {"cost_usd", "cost_micros", "credit", "credits", "billable_quantity", "price"}


@pytest.fixture
def meter():
    cost_meter.configure(enabled=True)
    cost_meter.bind("rt", IDENTITY)
    cost_meter.drain(10_000)
    for name in ("dropped", "unbound", "lost", "conflicts"):
        setattr(cost_meter, name, 0)
    yield cost_meter
    cost_meter.configure(enabled=False)
    cost_meter.unbind("rt")
    cost_meter.drain(10_000)


def body_of(sample, producer="runtime"):
    eid, body = envelope.build_cost_body(
        sample.identity,
        cost_kind=sample.cost_kind,
        cost_key=sample.cost_key,
        occurred_at=sample.occurred_at,
        provider=sample.provider,
        model=sample.model,
        usage_source=sample.usage_source,
        fields=sample.fields,
        producer_id=producer,
    )
    return eid, body, json.loads(body)


def llm(meter, key="k", i=1, o=2):
    meter.record_llm("rt", call_id=key, provider="p", model="m", input_tokens=i, output_tokens=o)


# -- envelope ------------------------------------------------------------------------------


def test_envelope_carries_full_identity_and_no_money(meter):
    llm(meter, "c1", 3, 4)
    eid, _, doc = body_of(meter.drain(1)[0])
    assert doc["event_type"] == "ai.cost.reported" != envelope.COGS_EVENT_TYPE
    assert doc["tenant_id"] == TENANT and doc["stream_id"] == "biz" and doc["ai_session_id"] == "rt"
    assert doc["payload"]["generation"] == "g1" and doc["event_id"] == eid
    assert doc["payload"]["input_tokens"] == 3 and doc["payload"]["usage_source"] == "provider"
    assert not FORBIDDEN & set(doc["payload"]) and not FORBIDDEN & set(doc)


def test_event_id_is_stable_and_cost_kind_and_generation_are_part_of_it(meter):
    llm(meter, "k")
    meter.record_tts(
        "rt",
        utterance_id="k",
        phrase_seq=0,
        attempt=1,
        provider="t",
        characters=1,
        audio_ms=1,
        synth_ms=1,
    )
    first, tts = meter.drain(2)
    assert body_of(first) == body_of(first)
    assert body_of(first)[0] != body_of(tts)[0]
    other_generation = ExecutionIdentity(**{**IDENTITY.model_dump(), "generation": "g2"})
    assert envelope.cost_event_id(IDENTITY, "LLM", "k") != envelope.cost_event_id(
        other_generation, "LLM", "k"
    )


def test_unknown_llm_usage_is_null_and_never_marked_measured(meter):
    meter.record_llm(
        "rt", call_id="c", provider="p", model="m", input_tokens=None, output_tokens=None
    )
    payload = body_of(meter.drain(1)[0])[2]["payload"]
    assert payload["input_tokens"] is None and payload["output_tokens"] is None
    assert payload["usage_source"] == "unknown"


# -- TTS: one row per call, keyed utterance:phrase:attempt ------------------------------------


class _Tts:
    name = "fake"

    def stream_audio(self, phrase, *, session_id, utterance_id):
        yield SimpleNamespace(duration_ms=300)
        yield SimpleNamespace(duration_ms=200)


def _run_tts(phrase_seq, text="xin chao", tts=None):
    fake = SimpleNamespace(_tts=tts or _Tts())
    phrase = SimpleNamespace(seq=phrase_seq, text=text)
    return list(StreamOrchestrator._tts_audio(fake, phrase, "rt", "utt1"))


def test_tts_phrases_of_one_utterance_make_separate_rows_and_a_retry_a_new_attempt(meter):
    _run_tts(0)
    _run_tts(1, "cau hai")
    _run_tts(1, "cau hai")
    samples = meter.drain(10)
    assert [s.cost_key for s in samples] == ["utt1:0:1", "utt1:1:1", "utt1:1:2"]
    assert samples[1].fields["characters"] == len("cau hai")
    assert samples[0].fields["audio_ms"] == 500


def test_tts_row_is_recorded_even_when_the_provider_fails_midway(meter):
    class Boom(_Tts):
        def stream_audio(self, phrase, *, session_id, utterance_id):
            yield SimpleNamespace(duration_ms=100)
            raise RuntimeError("tts down")

    with pytest.raises(RuntimeError):
        _run_tts(0, "a", Boom())
    assert meter.drain(1)[0].fields["audio_ms"] == 100


# -- LLM: one call -> exactly one cost row, never the legacy COGS -------------------------------


class _Client:
    model = "m1"

    def __init__(self, usage):
        self._usage = usage
        self.include_usage = None

    def chat_stream(self, req, usage=None):
        self.include_usage = req.include_usage
        yield "a"
        yield "b"
        if usage is not None and self._usage:
            usage.update(self._usage)


def _llm_call(usage):
    client = _Client(usage)
    engine = OpenAICompatLLMEngine(client)
    request = LLMRequest.from_prompt("hi")
    chunks = list(engine.stream_chunks(request, session_id="rt", utterance_id="u"))
    return client, chunks


def test_one_llm_call_emits_one_cost_row_and_zero_legacy_cogs(meter):
    service = UsageEvidence(SimpleNamespace(), UsageEvidenceSettings())  # legacy flag default off
    client, chunks = _llm_call({"prompt_tokens": 7, "completion_tokens": 9})
    assert [c.text for c in chunks] == ["a", "b"] and chunks[-1].is_final and client.include_usage
    samples = meter.drain(10)
    assert len(samples) == 1 and samples[0].fields == {"input_tokens": 7, "output_tokens": 9}
    assert samples[0].model == "m1"
    service.record_cogs(IDENTITY, model_id="m", input_tokens=1, output_tokens=1)
    assert len(service.cogs) == 0  # the old path cannot reach ai.usage.reported


def test_llm_without_provider_usage_reports_unknown_not_a_guess(meter):
    _llm_call(None)
    sample = meter.drain(1)[0]
    assert sample.usage_source == "unknown" and sample.fields["input_tokens"] is None


def test_legacy_cogs_only_when_its_own_flag_is_set():
    on = UsageEvidence(SimpleNamespace(), UsageEvidenceSettings(legacy_cogs_enabled=True))
    on.record_cogs(IDENTITY, model_id="m", input_tokens=1, output_tokens=1)
    assert len(on.cogs) == 1
    assert UsageEvidenceSettings.from_env({}).legacy_cogs_enabled is False
    assert UsageEvidenceSettings.from_env({}).cost_enabled is False


def test_producer_off_is_inert_and_does_not_touch_the_stream():
    cost_meter.configure(enabled=False)
    cost_meter.bind("rt", IDENTITY)
    client, chunks = _llm_call({"prompt_tokens": 1, "completion_tokens": 1})
    assert client.include_usage is False  # no stream_options requested
    assert len(chunks) == 2 and len(cost_meter.drain(10)) == 0
    cost_meter.unbind("rt")


# -- binding, avatar sessions, rollover ----------------------------------------------------------


def test_unbound_session_is_counted_not_buffered(meter):
    meter.record_llm("other", call_id="c", provider="p", model="m", input_tokens=1, output_tokens=1)
    assert meter.unbound == 1 and meter.drain(1) == []


def test_avatar_rollover_makes_one_row_per_provider_session_and_close_is_idempotent(meter):
    meter.avatar_open("rt", "ps1")
    meter.avatar_close("rt", "ps1")  # rollover ends the first
    meter.avatar_open("rt", "ps2")
    meter.avatar_close("rt", "ps2")  # stop ends the second
    meter.avatar_close("rt", "ps2")  # duplicate teardown
    rows = meter.drain(10)
    assert [r.cost_key for r in rows] == ["ps1", "ps2"]
    payload = body_of(rows[0])[2]["payload"]
    assert payload["cost_kind"] == "AVATAR" and payload["seconds"] >= 0
    assert set(payload["window"]) == {"started_at", "ended_at"}


def test_buffer_overflow_drops_and_counts_without_raising(meter):
    meter.configure(enabled=True, buffer=2)
    for i in range(5):
        llm(meter, str(i))
    assert meter.dropped == 3 and len(meter.drain(10)) == 2
    meter.configure(enabled=True)


def test_a_broken_record_never_raises_into_the_live_path(meter, monkeypatch):
    monkeypatch.setattr(meter, "_buf", None)
    llm(meter, "c")
    assert meter.lost == 1


# -- sender: durable, best effort ------------------------------------------------------------------


class FakeOutbox:
    """Emulates the (identity, kind, interval_id) unique key + ON CONFLICT DO NOTHING."""

    def __init__(self, fail=0):
        self.rows, self.fail = {}, fail

    async def enqueue_cost(self, identity, *, event_id, cost_kind, cost_key, occurred_at, body):
        if self.fail:
            self.fail -= 1
            raise ConnectionError("pg down")
        key = (identity.model_dump_json(), f"cost:{cost_kind}", cost_key)
        if key in self.rows:
            return "duplicate" if self.rows[key] == body else "conflict"
        self.rows[key] = body
        return "stored"


async def test_replay_is_idempotent_and_cost_kinds_do_not_overwrite_each_other(meter):
    for _ in range(2):  # the same facts twice (replay)
        llm(meter, "k")
        meter.record_tts(
            "rt",
            utterance_id="k",
            phrase_seq=0,
            attempt=1,
            provider="t",
            characters=3,
            audio_ms=4,
            synth_ms=5,
        )
        meter.avatar_open("rt", "k")
        meter.avatar_close("rt", "k")
    outbox = FakeOutbox()
    await UsageSender(outbox, UsageEvidenceSettings(), cost=meter).drain_cost()
    # LLM key "k" and AVATAR key "k" collide on cost_key alone; kind keeps them apart.
    assert sorted(k[1] for k in outbox.rows) == ["cost:AVATAR", "cost:LLM", "cost:TTS"]


async def test_same_key_different_bytes_is_a_conflict_not_a_silent_drop(meter):
    outbox = FakeOutbox()
    sender = UsageSender(outbox, UsageEvidenceSettings(), cost=meter)
    llm(meter, "k", 1, 2)
    await sender.drain_cost()
    llm(meter, "k", 9, 9)
    await sender.drain_cost()
    assert meter.conflicts == 1 and len(outbox.rows) == 1


async def test_outbox_failure_never_raises_and_is_retried_then_counted_lost(meter):
    outbox = FakeOutbox(fail=99)
    sender = UsageSender(outbox, UsageEvidenceSettings(), cost=meter)
    llm(meter, "k")
    for _ in range(4):
        await sender.drain_cost()  # must not raise
    assert meter.lost == 1 and not outbox.rows


async def test_a_transient_outbox_failure_is_retried_and_stored(meter):
    outbox = FakeOutbox(fail=1)
    sender = UsageSender(outbox, UsageEvidenceSettings(), cost=meter)
    llm(meter, "k2")
    for _ in range(2):
        await sender.drain_cost()
    assert len(outbox.rows) == 1 and meter.lost == 0


def test_meter_is_a_fresh_default_off_object():
    assert CostMeter().enabled is False
