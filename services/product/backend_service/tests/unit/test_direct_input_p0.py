"""P0-FB-015 Runtime half: direct say disabled and direct-event rejections on P0.

Real create_app and authenticated HTTP (reuses 005/006's fixture); spies on the
downstream calls prove nothing reached the safety screen, LLM, TTS or boundary.
"""

import logging
import time
from unittest.mock import AsyncMock, Mock

import pytest

from . import test_approved_speech_active as speech_tests
from .test_p0_comment_contract import event
from .test_safety_gate_active import canonical

case_factory = speech_tests.case_factory
SECRET_TEXT = "ZZ-secret-viewer-text"
DELIVERY = [False, True]


def spies(case, monkeypatch):
    prepare = AsyncMock(wraps=case.d.approved_speech.prepare)
    screen = AsyncMock(wraps=case.d.event_ingestion.screen_direct_generation)
    monkeypatch.setattr(case.d.approved_speech, "prepare", prepare)
    monkeypatch.setattr(case.d.event_ingestion, "screen_direct_generation", screen)
    return prepare, screen


async def legacy_session(case):
    start = await case.client.post("/api/v1/sessions", json={})
    assert start.status_code == 200, start.text
    return start.json()["session_id"]


def legacy_event(**changes):
    return {
        "event_id": "legacy-1",
        "source_stream_id": "business-1",
        "occurred_at": time.time(),
        "type": "viewer.comment",
        "platform": "facebook",
        "viewer": {"viewer_id": "viewer-1"},
        "payload": {"text": "xin chào"},
        **changes,
    }


async def post_events(case, raw, *, sid=None, delivery=False):
    return await case.client.post(
        f"/api/v1/sessions/{sid or case.sid}/events",
        json={"events": [raw], "delivery_outcomes_v1": delivery},
    )


def one(response):
    assert response.status_code == 200, response.text
    return response.json()["events"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize("generate", [False, True])
async def test_p0_say_rejected_typed_before_screen_llm_tts_boundary(
    case_factory, monkeypatch, caplog, generate
):
    case = await case_factory()
    prepare, screen = spies(case, monkeypatch)
    with caplog.at_level(logging.WARNING):
        response = await case.client.post(
            f"/api/v1/sessions/{case.sid}/say",
            json={"text": SECRET_TEXT, "generate": generate},
        )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "direct_input_disabled"
    prepare.assert_not_called()
    screen.assert_not_called()
    assert not case.llm.started.is_set()
    assert not case.tts.calls
    assert not case.events  # nothing reached the hub either
    audit = [r.getMessage() for r in caplog.records if "audit_event=" in r.getMessage()]
    assert len(audit) == 1
    assert "audit_event=direct_input_rejected" in audit[0]
    assert "reason=direct_input_disabled" in audit[0]
    assert f"session={case.sid}" in audit[0]
    assert SECRET_TEXT not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("generate", [False, True])
async def test_forged_body_contract_cannot_select_p0_on_a_legacy_session(
    case_factory, monkeypatch, generate
):
    case = await case_factory()
    prepare, screen = spies(case, monkeypatch)
    legacy = await legacy_session(case)
    forged = {"execution_contract": "p0.execution.v1", "contract_version": "p0.v1"}
    response = await case.client.post(
        f"/api/v1/sessions/{legacy}/say",
        json={"text": SECRET_TEXT, "generate": generate, **forged},
    )
    # Not rejected as direct input: it reaches the 005 path (no binding here).
    assert response.status_code == 409
    assert response.json()["error"]["code"] != "direct_input_disabled"
    (screen if generate else prepare).assert_called_once()


@pytest.mark.asyncio
async def test_legacy_say_keeps_005_validation_unchanged(case_factory):
    case = await case_factory()
    legacy = await legacy_session(case)
    response = await case.client.post(
        f"/api/v1/sessions/{legacy}/say", json={"text": "xin chào", "generate": False}
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "missing_binding"
    assert not case.tts.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", DELIVERY)
async def test_p0_events_legacy_event_rejected_with_typed_reason(
    case_factory, monkeypatch, delivery
):
    case = await case_factory()
    await canonical(case)
    coordinator = Mock(wraps=case.d.coordinator.ingest)
    monkeypatch.setattr(case.d.coordinator, "ingest", coordinator)
    got = one(await post_events(case, legacy_event(), delivery=delivery))
    assert got["status"] == "rejected"
    assert got["reason"] == "p0_contract_required"
    coordinator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", DELIVERY)
@pytest.mark.parametrize(
    "field", ["tenant_id", "business_session_id", "connected_account_id", "external_session_id"]
)
async def test_p0_events_binding_mismatch_rejected_with_typed_reason(
    case_factory, monkeypatch, delivery, field
):
    case = await case_factory()
    await canonical(case)
    coordinator = Mock(wraps=case.d.coordinator.ingest)
    monkeypatch.setattr(case.d.coordinator, "ingest", coordinator)
    raw = event(**{field: "forged"}).model_dump()
    got = one(await post_events(case, raw, delivery=delivery))
    assert got["status"] == "rejected"
    assert got["reason"] == f"p0_{field}_mismatch"
    coordinator.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", DELIVERY)
async def test_p0_events_missing_session_binding_and_source_stream_mismatch(case_factory, delivery):
    case = await case_factory()
    # P0 session that has no platform_event_binding yet.
    got = one(await post_events(case, event().model_dump(), delivery=delivery))
    assert got["status"] == "rejected" and got["reason"] == "p0_binding_missing"
    await canonical(case)
    raw = event(source_stream_id="other-stream").model_dump()
    got = one(await post_events(case, raw, delivery=delivery))
    assert got["status"] == "rejected" and got["reason"] == "p0_source_stream_id_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["moderation_ref", "source_message_id", "tenant_id", "viewer"])
async def test_p0_events_missing_provenance_rejected_at_the_schema_boundary(case_factory, field):
    case = await case_factory()
    await canonical(case)
    raw = event().model_dump()
    raw.pop(field)
    response = await post_events(case, raw)
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_event_forged_contract_fields_do_not_select_p0(case_factory):
    case = await case_factory()
    legacy = await legacy_session(case)
    forged = legacy_event(
        execution_contract="p0.execution.v1",
        payload={"text": "xin chào", "execution_contract": "p0.execution.v1"},
    )
    response = await post_events(case, forged, sid=legacy)
    if response.status_code == 200:  # extras ignored: judged by session meta alone
        assert one(response).get("reason") not in {"p0_contract_required", "p0_binding_missing"}
    else:  # or the strict schema refuses the forged field
        assert response.status_code == 422
    # The real P0 session still rejects the same legacy-shaped event.
    await canonical(case)
    got = one(await post_events(case, forged))
    assert got["reason"] == "p0_contract_required"


@pytest.mark.asyncio
async def test_p0_events_rejection_is_audited_without_viewer_text(case_factory, monkeypatch):
    case = await case_factory()
    await canonical(case)
    audit = AsyncMock()
    monkeypatch.setattr(
        case.d.event_ingestion, "_pg_store", Mock(enabled=True, insert_audit_event=audit)
    )
    raw = event(SECRET_TEXT, tenant_id="forged").model_dump()
    assert one(await post_events(case, raw))["reason"] == "p0_tenant_id_mismatch"
    audit.assert_called_once()
    assert audit.call_args.args[0] == "event_ingress.rejected"
    assert audit.call_args.kwargs["detail"]["reason"] == "p0_tenant_id_mismatch"
    assert SECRET_TEXT not in str(audit.call_args)
