"""P0 canonical comment bound, binding, and reducer provenance."""

import time

import pytest
from pydantic import ValidationError

from backend.application.db.memory_session_store import InMemorySessionStore
from backend.application.platform_events import P0SessionBinding, PlatformEvent
from backend.application.platform_events.ingestion import PlatformEventIngestionService


BINDING = {
    "contract_version": "p0.v1",
    "tenant_id": "tenant-1",
    "business_session_id": "business-1",
    "platform": "facebook",
    "connected_account_id": "page-1",
    "external_session_id": "live-1",
}


def event(text="xin chào", **overrides):
    values = {
        **BINDING,
        "event_id": "event-1",
        "source_stream_id": "business-1",
        "source_message_id": "message-1",
        "moderation_ref": "queue-1",
        "occurred_at": time.time(),
        "type": "viewer.comment",
        "viewer": {"viewer_id": "viewer-1"},
        "payload": {"text": text},
    }
    values.update(overrides)
    return PlatformEvent(**values)


@pytest.mark.parametrize(
    "text",
    [
        "a",
        *["a" * n for n in (499, 500, 501, 999, 1000)],
        "ắ" * 1000,
        "😀" * 1000,
        "a\u0301" * 500,
        "a  b",
    ],
)
def test_exact_text_to_1000_code_points(text):
    assert event(text).payload.text == text


@pytest.mark.parametrize(
    "text", ["", "   ", "a" * 1001, "ắ" * 1001, "😀" * 1001, "a\u0301" * 500 + "a"]
)
def test_empty_or_overbound_rejected(text):
    with pytest.raises(ValidationError):
        event(text)


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "business_session_id",
        "connected_account_id",
        "external_session_id",
        "source_message_id",
        "moderation_ref",
    ],
)
def test_missing_required_p0_provenance_rejected(field):
    with pytest.raises(ValidationError):
        event(**{field: None})


def test_missing_viewer_and_source_time_rejected():
    with pytest.raises(ValidationError):
        event(viewer=None)
    with pytest.raises(ValidationError):
        event(occurred_at=0)


def test_prepared_binding_rejects_blank_identity():
    with pytest.raises(ValidationError):
        P0SessionBinding(**{**BINDING, "connected_account_id": " "})


class ReducerProbe:
    def __init__(self):
        self.comments = []

    def notify_new_events(self, session_id, comment):
        self.comments.append(comment)


class AuditProbe:
    enabled = True

    def __init__(self):
        self.payloads = []

    async def insert_viewer_msg(self, *args, **kwargs):
        self.payloads.append(kwargs["payload"])


@pytest.mark.asyncio
async def test_p0_binding_and_reducer_provenance():
    store = InMemorySessionStore()
    await store.set("runtime-1", {"platform_event_binding": BINDING})
    reducer = ReducerProbe()
    audit = AuditProbe()
    service = PlatformEventIngestionService(store=store, reducer=reducer, pg_store=audit)
    valid = event("ắ😀" * 500)
    result = await service.ingest("runtime-1", [valid])
    assert result["accepted"] == 1
    assert reducer.comments[0].text == valid.payload.text
    assert reducer.comments[0].provenance == {
        **BINDING,
        "source_message_id": "message-1",
        "source_stream_id": "business-1",
        "event_id": "event-1",
        "viewer_id": "viewer-1",
        "occurred_at": valid.occurred_at,
        "moderation_ref": "queue-1",
    }
    pending = (await store.get("runtime-1"))["pending_platform_chat"]
    assert pending[0]["text"] == valid.payload.text
    assert pending[0]["provenance"] == reducer.comments[0].provenance
    assert audit.payloads == [reducer.comments[0].provenance]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    ["tenant_id", "business_session_id", "platform", "connected_account_id", "external_session_id"],
)
async def test_p0_wrong_binding_rejected(field):
    store = InMemorySessionStore()
    await store.set("runtime-1", {"platform_event_binding": BINDING})
    service = PlatformEventIngestionService(store=store)
    result = await service.ingest("runtime-1", [event(**{field: "wrong"})])
    assert result["events"][0]["reason"] == f"p0_{field}_mismatch"


@pytest.mark.asyncio
async def test_p0_missing_binding_and_legacy_cannot_enter_p0_session():
    store = InMemorySessionStore()
    await store.set("runtime-1", {"platform_event_binding": BINDING})
    service = PlatformEventIngestionService(store=store)
    legacy = event().model_dump(
        exclude={
            "contract_version",
            "tenant_id",
            "business_session_id",
            "connected_account_id",
            "external_session_id",
            "source_message_id",
            "moderation_ref",
        }
    )
    assert (await service.ingest("runtime-1", [PlatformEvent(**legacy)]))["events"][0][
        "reason"
    ] == "p0_contract_required"
    await store.set("legacy-1", {"status": "active"})
    assert (await service.ingest("legacy-1", [PlatformEvent(**{**legacy, "event_id": "legacy"})]))[
        "accepted"
    ] == 1
    assert (await service.ingest("legacy-1", [event(event_id="p0-other")]))["events"][0][
        "reason"
    ] == "p0_binding_missing"
