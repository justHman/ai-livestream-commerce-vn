"""P0-FB-005: real app composition, HTTP binding/attach and active dispatch.

Only authoring storage and media/LLM providers are controlled doubles. The
DirectorRuntime, Coordinator, speech boundary and chunker are real.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
import threading

import httpx
import pytest
import pytest_asyncio

from avatar.engines.base import FullPipelineBackend, StartResult
from avatar.engines.mock import MockRenderBackend
from avatar.engines.windows import AudioWindow
from backend.application.director.decision import Decision
from approved_speech_helpers import CLAIM, TEXT, Source
from backend.bootstrap import app_factory
from backend.config import AppConfig


class LLM:
    name = "controlled"

    def __init__(self):
        self.output = CLAIM
        self.started = threading.Event()
        self.release = threading.Event()
        self.release.set()
        self.complete = False

    def stream_chunks(self, request, **kwargs):
        self.started.set()
        midpoint = len(self.output) // 2
        yield SimpleNamespace(text=self.output[:midpoint])
        assert self.release.wait(5), "test failed to release LLM"
        self.complete = True
        yield SimpleNamespace(text=self.output[midpoint:])


class TTS:
    def __init__(self, events):
        self.calls = []
        self.events = events

    def stream_audio(self, chunk, **kwargs):
        assert any(e.get("chunk_id") == chunk.id for e in self.events), "TTS before evidence"
        self.calls.append(chunk.text)
        yield AudioWindow(
            session_id=chunk.session_id,
            utterance_id=chunk.utterance_id,
            seq=0,
            sample_rate=24000,
            duration_ms=10,
            pcm=b"\x01\x00" * 240,
            text_span=chunk.text,
            is_final=True,
        )


class Cloud(FullPipelineBackend):
    def __init__(self, events):
        self.calls = []
        self.events = events

    def start(self, opts):
        return StartResult("cloud-session", "", "")

    def stop(self, sid):
        pass

    def interrupt(self, sid):
        pass

    def say(self, sid, text, generate=True):
        assert generate is False, "opaque cloud generation bypasses validation"
        assert any(e["type"] == "speech.content_validated" for e in self.events)
        self.calls.append(text)
        return text


@pytest_asyncio.fixture
async def case_factory(monkeypatch):
    cases = []
    monkeypatch.setenv("BACKEND_API_TOKEN", "")
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("TEXT_CHUNK_POLICY", "fixed")
    monkeypatch.setenv("LIVEKIT_PUBLISH", "false")

    async def create(*, cloud=False, bind=True):
        source, llm, events = Source(), LLM(), []
        tts = TTS(events)
        backend = Cloud(events) if cloud else MockRenderBackend()
        monkeypatch.setattr(AppConfig, "build_render_backend", lambda _: backend)
        monkeypatch.setattr(
            app_factory, "v1_engine_manager", lambda _: SimpleNamespace(llm=llm, tts=tts)
        )
        monkeypatch.setattr(app_factory, "_build_pg_store", lambda _: None)
        monkeypatch.setattr(app_factory, "_build_script_authoring", lambda *_: source)
        app = app_factory.create_app(
            config=AppConfig(
                render_backend="mock",
                app_env="dev",
                director_enabled=True,
            )
        )
        d = app.state.container

        async def emit(sid, event):
            events.append(event)

        d.hub.emit = emit
        d.coordinator._hub = d.hub
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        start = await client.post(
            "/api/v1/sessions",
            json={
                "execution_contract": "p0.execution.v1",
                "tenant_id": "tenant-1",
                "business_session_id": "business-1",
                "generation": "generation-1",
            },
        )
        assert start.status_code == 200, start.text
        sid = start.json()["session_id"]
        case = SimpleNamespace(
            d=d,
            client=client,
            source=source,
            llm=llm,
            tts=tts,
            backend=backend,
            sid=sid,
            events=events,
        )
        cases.append(case)
        if bind:
            result = await client.put(
                f"/api/v1/sessions/{sid}/script-set",
                json={
                    "script_set_id": source.set.id,
                    "tenant_id": "tenant-1",
                    "business_session_id": "business-1",
                },
            )
            assert result.status_code == 200, result.text
            attached = await client.post(
                f"/api/v1/sessions/{sid}/attach",
                json={
                    "products": [{"id": "unapproved", "name": "RAW", "price": 999}],
                },
            )
            assert attached.status_code == 200, attached.text
            assert attached.json()["products"] == ["product-1"]
            assert sid not in d.coordinator._activated
            assert d.director.get_session(sid).approved_envelope is not None
            # Drive the real dispatch deterministically, without its scheduler.
            task = d.coordinator._playback_tasks[sid]
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return case

    yield create
    for case in cases:
        case.llm.release.set()
        case.d.coordinator.stop_all()
        await case.client.aclose()
        case.backend.stop_all()
    await asyncio.sleep(0)


def decision(case, *, generated=False):
    return Decision(
        action="answer_cluster" if generated else "introduce_product",
        product_id="product-1",
        prompt="Bao lâu giao hàng?" if generated else "sell raw facts",
        revision_token=case.d.director.current_generation_token(case.sid),
    )


async def prepare(case, turn):
    coordinator = case.d.coordinator
    coordinator._decision_queue[case.sid].append(turn)
    await coordinator._prepare_turn(case.sid, turn)


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [False, True])
async def test_composition_prepared_locked_text_and_evidence(case_factory, cloud):
    case = await case_factory(cloud=cloud)
    turn = decision(case)
    await prepare(case, turn)
    assert turn.prepared_script == TEXT
    assert not case.tts.calls
    assert not case.llm.started.is_set()
    assert await case.d.coordinator._maybe_speak(case.sid, turn)
    assert case.backend.calls if cloud else case.tts.calls
    completed = case.d.coordinator._completed_speech[case.sid]
    assert completed["script"] == TEXT
    assert completed["validation"]["approved_version_id"] == case.source.version.id
    assert completed["validation"]["approval_hash"] == case.source.approval.approval_hash


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [False, True])
async def test_generated_speech_buffered_before_any_audio(case_factory, cloud):
    case = await case_factory(cloud=cloud)
    case.llm.release.clear()
    turn = decision(case, generated=True)
    pending = asyncio.create_task(case.d.coordinator._maybe_speak(case.sid, turn))
    assert await asyncio.to_thread(case.llm.started.wait, 2)
    assert not case.tts.calls
    assert not getattr(case.backend, "calls", [])
    case.llm.release.set()
    await pending
    assert case.llm.complete
    assert (case.backend.calls if cloud else case.tts.calls) == [CLAIM]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text",
    [
        "Giá chỉ 999000 VND.",
        "Giao hàng miễn phí trong một giờ.",
        CLAIM + " Bảo hành trọn đời.",
        "Sản phẩm chữa mọi bệnh.",
    ],
)
@pytest.mark.parametrize("route", ["prepared", "generated", "direct"])
async def test_unsupported_claims_never_reach_tts(case_factory, text, route):
    case = await case_factory()
    if route == "direct":
        result = await case.client.post(
            f"/api/v1/sessions/{case.sid}/say",
            json={
                "text": text,
                "generate": False,
            },
        )
        assert result.status_code == 409
    else:
        turn = decision(case, generated=True)
        if route == "prepared":
            turn.prepared_script = text
        else:
            case.llm.output = text
        await prepare(case, turn)
        await case.d.coordinator._maybe_speak(case.sid, turn)
    assert not case.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "version",
        "source_version",
        "text",
        "dependencies",
        "expired",
        "scope",
        "hash",
        "facts",
    ],
)
async def test_binding_rechecked_before_prepared_dispatch(case_factory, change):
    case = await case_factory()
    turn = decision(case)
    await prepare(case, turn)
    meta = dict(await case.d.store.get(case.sid))
    if change == "missing":
        meta.pop("script_set_binding")
    elif change in ("version", "text"):
        key = "approved_version_id" if change == "version" else "spoken_text"
        meta["script_set_binding"]["products"][0][key] = "tampered"
    elif change == "dependencies":
        case.source.deps = replace(case.source.deps, rule_set_version="rules-v2")
    elif change == "source_version":
        case.source.version.id = "new-approved-version"
    elif change == "expired":
        case.source.set.brief.facts_valid_until = "2020-01-01T00:00:00Z"
    elif change == "scope":
        meta["execution_contract"]["tenant_id"] = "another-tenant"
    elif change == "hash":
        case.source.approval.approval_hash = "0" * 64
    else:
        case.source.set.brief.product_facts["product-1"]["allowed_claims"] = ["Invented"]
    await case.d.store.set(case.sid, meta)
    await case.d.coordinator._maybe_speak(case.sid, turn)
    assert not case.tts.calls
    assert turn.is_cancelled


@pytest.mark.asyncio
async def test_locked_prepared_bytes_cannot_change(case_factory):
    case = await case_factory()
    turn = decision(case)
    await prepare(case, turn)
    turn.prepared_script = TEXT.replace("ba ngày", "một giờ")
    await case.d.coordinator._maybe_speak(case.sid, turn)
    assert not case.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("cancel", ["interrupt", "stop"])
async def test_late_generated_completion_is_fenced(case_factory, cloud, cancel):
    case = await case_factory(cloud=cloud)
    case.llm.release.clear()
    pending = asyncio.create_task(
        case.client.post(
            f"/api/v1/sessions/{case.sid}/say",
            json={
                "text": "Bao lâu giao hàng?",
                "generate": True,
            },
        )
    )
    assert await asyncio.to_thread(case.llm.started.wait, 2)
    response = await case.client.post(f"/api/v1/sessions/{case.sid}/{cancel}")
    assert response.status_code == 200, response.text
    case.llm.release.set()
    response = await pending
    assert response.status_code == 409, response.text
    assert not case.tts.calls
    assert not getattr(case.backend, "calls", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("generate", [False, True])
async def test_direct_say_valid_content_and_version_evidence(case_factory, cloud, generate):
    case = await case_factory(cloud=cloud)
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/say",
        json={
            "text": "Bao lâu giao hàng?" if generate else TEXT,
            "generate": generate,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == (CLAIM if generate else TEXT)
    assert response.json()["validation"]["approved_version_id"] == case.source.version.id


@pytest.mark.asyncio
async def test_missing_binding_direct_and_attach_fail_closed(case_factory):
    case = await case_factory(bind=False)
    for route, body in [("say", {"text": TEXT, "generate": False}), ("attach", {"products": []})]:
        response = await case.client.post(f"/api/v1/sessions/{case.sid}/{route}", json=body)
        assert response.status_code == 409, response.text
    assert not case.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("cloud", [False, True])
async def test_late_director_preparation_is_fenced(case_factory, cloud):
    case = await case_factory(cloud=cloud)
    case.llm.release.clear()
    turn = decision(case, generated=True)
    pending = asyncio.create_task(prepare(case, turn))
    assert await asyncio.to_thread(case.llm.started.wait, 2)
    await case.d.coordinator.interrupt(case.sid)
    case.llm.release.set()
    await pending
    await case.d.coordinator._maybe_speak(case.sid, turn)
    assert turn.is_cancelled
    assert not case.tts.calls
    assert not getattr(case.backend, "calls", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalidate", ["cancel", "expire"])
async def test_pending_tts_audio_cannot_escape_after_invalidation(case_factory, invalidate):
    case = await case_factory()
    audio = []

    async def publish(window):
        audio.append(window)

    case.d.livekit_publishers = SimpleNamespace(publish=publish)
    original = case.tts.stream_audio

    def delayed_audio(chunk, **kwargs):
        # The orchestrator holds this first window pending a finality decision.
        yield from original(chunk, **kwargs)
        if invalidate == "cancel":
            case.d.approved_speech.cancel(case.sid)
        else:
            case.source.set.brief.facts_valid_until = "2020-01-01T00:00:00Z"
        # Provider error flushes pending audio; the publication guard must reject it.
        raise ConnectionError("late provider failure")

    case.tts.stream_audio = delayed_audio
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/say", json={"text": TEXT, "generate": False}
    )
    assert response.status_code == 409, response.text
    assert case.tts.calls
    assert not audio


@pytest.mark.asyncio
async def test_default_adaptive_chunker_preserves_locked_script(case_factory, monkeypatch):
    case = await case_factory()
    monkeypatch.setenv("TEXT_CHUNK_POLICY", "adaptive_vi")
    response = await case.client.post(
        f"/api/v1/sessions/{case.sid}/say", json={"text": TEXT, "generate": False}
    )
    assert response.status_code == 200, response.text
    assert response.json()["reply"] == TEXT
    assert case.tts.calls
