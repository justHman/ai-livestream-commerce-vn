"""Every path that stores a model or owner text keeps the brand in display_text and its
pronunciation in spoken_text; model prompts never see the pronunciation."""

from __future__ import annotations

import asyncio

import pytest
from test_service_impl_generation import (
    _fail_result,
    _FakeEngineManager,
    _make_service,
    _pass_result,
    _wait_for_state,
)

from backend.application.script_authoring.models import ScriptSource, ScriptState

BRIEF = {
    "product_facts": {
        "P1": {
            "product_name": "Áo thun basic",
            "product_info": {"brand": "Livento", "brand_pronunciation": "Lai-ven-to"},
        }
    }
}
# One answer for every call: a valid plan whose lines also serve as segment text.
BRANDED = "1. Mở đầu|Livento giới thiệu mẫu áo|600\n2. Nội dung|Lợi ích chính|600\n"


class _Llm:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return BRANDED


class _FirstSegmentFailsGate:
    """Fails the first segment check (so the repair path runs) and every full-script check."""

    def __init__(self) -> None:
        self.segment_calls = 0

    def run_segment(self, text, context):
        self.segment_calls += 1
        return _fail_result() if self.segment_calls == 1 else _pass_result()

    def run_full_script(self, segments, context):
        return _fail_result()


async def _set(service):
    created = await service.create_script_set(
        name="Phiên", transition_policy="ORDER_AGNOSTIC", product_ids=["P1"], brief=BRIEF
    )
    return created["id"]


async def _version(repos, set_id, source):
    for _ in range(500):
        item = await repos.items.get_by_product(set_id, "P1")
        version = repos.versions.rows.get(item.current_version_id or "")
        if version is not None and version.source is source:
            return version
        await asyncio.sleep(0.01)
    raise AssertionError(f"no {source} version")


async def _generated(llm):
    service, repos = _make_service(_FirstSegmentFailsGate(), engine_manager=_FakeEngineManager(llm))
    set_id = await _set(service)
    await service.start_generation(
        set_id=set_id, product_id="P1", target_duration_s=600, intent="selling", idempotency_key="g"
    )
    await _wait_for_state(repos, set_id, "P1", ScriptState.GATE_FAILED)
    return service, repos, set_id


@pytest.mark.asyncio
async def test_generation_and_segment_repair_store_the_pronunciation_as_spoken_text() -> None:
    _service, repos, set_id = await _generated(_Llm())
    item = await repos.items.get_by_product(set_id, "P1")
    version = repos.versions.rows[item.current_version_id]
    assert "Livento" in version.display_text and "Lai ven to" in version.spoken_text
    assert "Livento" not in version.spoken_text


@pytest.mark.asyncio
async def test_regeneration_stores_the_pronunciation_as_spoken_text() -> None:
    service, repos, set_id = await _generated(_Llm())
    await service.regenerate_segment(
        set_id=set_id, product_id="P1", segment_index=0, idempotency_key="r"
    )
    version = await _version(repos, set_id, ScriptSource.AI_REGENERATE)
    regenerated = version.spoken_text.split("\n\n")[0]
    assert "Lai ven to" in regenerated and "Livento" not in regenerated


@pytest.mark.asyncio
async def test_ai_fix_stores_the_pronunciation_as_spoken_text() -> None:
    service, repos, set_id = await _generated(_Llm())
    await service.fix_with_ai(set_id=set_id, product_id="P1", idempotency_key="f")
    version = await _version(repos, set_id, ScriptSource.AI_FIX)
    assert "Livento" in version.display_text and "Lai ven to" in version.spoken_text


@pytest.mark.asyncio
async def test_no_generation_repair_or_fix_prompt_carries_the_pronunciation() -> None:
    llm = _Llm()
    service, repos, set_id = await _generated(llm)
    await service.fix_with_ai(set_id=set_id, product_id="P1", idempotency_key="f")
    await _version(repos, set_id, ScriptSource.AI_FIX)
    assert not any("Lai ven to" in p or "Lai-ven-to" in p for p in llm.prompts)


@pytest.mark.asyncio
async def test_save_draft_applies_the_pronunciation_to_an_explicit_spoken_text() -> None:
    service, repos = _make_service(_FirstSegmentFailsGate())
    set_id = await _set(service)
    await service.save_draft(
        set_id=set_id,
        product_id="P1",
        display_text="Áo của Livento nhé.",
        spoken_text="Áo của Livento nhé.",
        revision=None,
    )
    item = await repos.items.get_by_product(set_id, "P1")
    assert repos.versions.rows[item.current_version_id].spoken_text == "Áo của Lai ven to nhé."
