"""Ordered session script generation (fake LLM, fake repos, real gate; no network)."""

from __future__ import annotations

import asyncio
import re
import time

import pytest
from test_service_impl_generation import (
    _FakeEngineManager,
    _FakeRepos,
    ScriptAuthoringConfig,
    ScriptAuthoringServiceImpl,
)

from backend.application.clients.llm.openai_compatible import LLMClientError
from backend.application.script_authoring.generation.ordered_units import (
    BoundedLLM,
    LLMDeadlineError,
    clean_unit_text,
    plan_roles,
)
from backend.application.script_authoring.models import ScriptState
from backend.application.script_authoring.service import ScriptAuthoringError
from backend.application.script_authoring.units import split_units

BRIEF = {
    "title": "Live thử nghiệm",
    "shop_name": "Shop A",
    "product_facts": {
        "P2": {
            "product_name": "Kem ABC",
            "prices": ["299.000đ"],
            "allowed_claims": ["Kem dưỡng ẩm sâu cho tóc khô.", "Thiết kế gọn nhẹ dễ mang theo."],
        },
        "P1": {
            "product_name": "Gel XYZ",
            "prices": ["150.000đ"],
            "allowed_claims": ["Gel làm sạch nhẹ nhàng mỗi ngày.", "Chai nhỏ gọn dùng được lâu."],
        },
    },
}

# Distinct, gate-clean Vietnamese per role/product (no phrase shared by 4+ units).
TEXT = {
    (
        "P2",
        "opening",
    ): "Chào cả nhà, chào mừng mọi người đã ghé buổi live của Shop A hôm nay, mình rất vui được gặp lại mọi người.",
    (
        "P2",
        "intro",
    ): "Đầu tiên mình xin giới thiệu Kem ABC, món quen thuộc của chị em yêu thích việc chăm sóc mái tóc mỗi ngày.",
    (
        "P2",
        "benefit0",
    ): "Kem dưỡng ẩm sâu cho tóc khô, thoa lên là thấy sợi tóc mềm mượt ngay sau vài phút sử dụng.",
    (
        "P2",
        "benefit1",
    ): "Thiết kế gọn nhẹ dễ mang theo, bỏ vào túi xách đi đâu cũng thấy thật tiện cho các bạn.",
    (
        "P2",
        "offer",
    ): "Hôm nay Kem ABC có giá chỉ 299.000đ cho mỗi hộp, một mức giá mình thấy rất dễ chịu.",
    (
        "P2",
        "trust",
    ): "Mình trấn an cả nhà nhé, shop kiểm hàng thật kỹ trước khi đóng gói gửi đến tận tay mọi người.",
    (
        "P2",
        "cta",
    ): "Bạn nào ưng ý thì bấm đặt hàng giúp mình nhé, sau đó mình sẽ sang món tiếp theo ngay đây.",
    (
        "P1",
        "intro",
    ): "Nào, chuyển sang món kế tiếp là Gel XYZ, một lựa chọn nhẹ nhàng cho những ngày bạn muốn thư giãn.",
    (
        "P1",
        "benefit0",
    ): "Gel làm sạch nhẹ nhàng mỗi ngày, rửa xong vẫn thoáng và không bị căng rát khó chịu.",
    (
        "P1",
        "benefit1",
    ): "Chai nhỏ gọn dùng được lâu, một chai đủ cho cả tháng chăm sóc đều đặn của bạn.",
    (
        "P1",
        "offer",
    ): "Gel XYZ đang có giá 150.000đ thôi, ai cũng có thể thêm vào giỏ hôm nay một cách thoải mái.",
    (
        "P1",
        "trust",
    ): "Cả nhà cứ yên tâm, shop luôn đồng hành hỗ trợ đổi trả nếu bạn chưa hài lòng với món này.",
    (
        "P1",
        "cta",
    ): "Nếu hợp thì chốt đơn ngay trong lúc livestream để nhận hàng sớm cùng quà nhỏ từ shop nhé.",
    (
        "P1",
        "closing",
    ): "Cảm ơn mọi người đã theo dõi suốt buổi, hẹn gặp lại cả nhà ở những buổi live thật vui sau này.",
}


class FakeLLM:
    """Answers each unit prompt by its role line; records every prompt."""

    def __init__(self, empty_for=None, raises=None):
        self.prompts = []
        self.empty_for = empty_for or set()
        self.raises = raises

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises
        role_line = re.search(r"\((\d+)/(\d+)\): ([^.]+)\.", prompt)
        title = role_line.group(3)
        names = {"Kem ABC": "P2", "Gel XYZ": "P1"}
        pid = next(p for n, p in names.items() if f"Tên sản phẩm: {n}" in prompt)
        role = {
            "Mở đầu phiên live": "opening",
            "Giới thiệu sản phẩm": "intro",
            "Điểm nổi bật": "benefit",
            "Giá và ưu đãi": "offer",
            "Tạo niềm tin": "trust",
            "Chốt đơn": "cta",
            "Lời kết phiên live": "closing",
        }[title]
        if role == "benefit":
            # the prompt carries exactly one allowed claim: pick the matching text
            role = (
                "benefit0"
                if "Thông tin được phép nói: " + BRIEF["product_facts"][pid]["allowed_claims"][0]
                in prompt
                else "benefit1"
            )
        if (pid, role) in self.empty_for:
            return ""
        return TEXT[(pid, role)]


def _service(llm, **config):
    repos = _FakeRepos()
    service = ScriptAuthoringServiceImpl(
        repos,
        config=ScriptAuthoringConfig(**config),
        gate=ScriptAuthoringServiceImpl._default_gate(),
        engine_manager=_FakeEngineManager(llm_fn=llm),
    )
    return service, repos


async def _new_set(service, policy="ORDER_AWARE"):
    # owner order P2, P1 is the OPPOSITE of the lexical id order
    return (
        await service.create_script_set(
            name="Phiên", transition_policy=policy, product_ids=["P2", "P1"], brief=BRIEF
        )
    )["id"]


async def _run(service, repos, set_id, key="k1"):
    started = await service.start_batch_generation(
        set_id=set_id,
        product_ids=[],
        target_duration_s=600,
        idempotency_key=key,
        ordered_units=True,
    )
    for _ in range(600):
        _batch, state = repos.batches.rows[started["batch_id"]]
        if state.status in ("completed", "partial_completed", "failed", "cancelled"):
            break
        await asyncio.sleep(0.02)
    return started["batch_id"]


async def _text(repos, set_id, pid):
    item = await repos.items.get_by_product(set_id, pid)
    version = await repos.versions.get(item.current_version_id)
    return item, version


@pytest.mark.asyncio
async def test_set_is_written_as_ordered_units_in_owner_order() -> None:
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)

    first, v_first = await _text(repos, set_id, "P2")
    last, v_last = await _text(repos, set_id, "P1")
    assert first.state is ScriptState.REVIEWABLE and last.state is ScriptState.REVIEWABLE
    units_first, units_last = split_units(v_first.spoken_text), split_units(v_last.spoken_text)
    # opening only in the FIRST product, closing only in the LAST, one claim per selling point
    assert units_first == tuple(
        TEXT[("P2", r)]
        for r in ("opening", "intro", "benefit0", "benefit1", "offer", "trust", "cta")
    )
    assert units_last == tuple(
        TEXT[("P1", r)]
        for r in ("intro", "benefit0", "benefit1", "offer", "trust", "cta", "closing")
    )
    # facts reach the prompts; the bridge names the neighbours only for an ORDER_AWARE set
    by_role = {p: p for p in llm.prompts}
    assert any("299.000đ" in p for p in by_role)
    assert any("lời nối từ sản phẩm trước (Kem ABC)" in p for p in by_role)
    assert any("sản phẩm tiếp theo là Gel XYZ" in p for p in by_role)
    assert len(llm.prompts) == 14  # one call per unit, no planning call, no retries needed

    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    assert snapshot["outcome"] == "succeeded" and snapshot["total"] == 2 and snapshot["done"] == 2
    assert [p["status"] for p in snapshot["products"]] == ["done", "done"]


@pytest.mark.asyncio
async def test_order_agnostic_set_never_names_neighbouring_products() -> None:
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service, policy="ORDER_AGNOSTIC")
    await _run(service, repos, set_id)
    assert not any("sản phẩm trước (" in p or "sản phẩm tiếp theo là" in p for p in llm.prompts)


@pytest.mark.asyncio
async def test_failed_unit_keeps_the_other_units_and_blocks_approval() -> None:
    llm = FakeLLM(empty_for={("P1", "trust")})
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)

    item, version = await _text(repos, set_id, "P1")
    units = split_units(version.spoken_text)
    assert item.state is ScriptState.GATE_FAILED
    assert len(units) == 7 and units[3] == TEXT[("P1", "offer")]
    assert units[4].startswith("<Phần này chưa soạn được")  # visible, gate-failing placeholder
    kept = [u for i, u in enumerate(units) if i != 4]
    assert kept == [
        TEXT[("P1", r)] for r in ("intro", "benefit0", "benefit1", "offer", "cta", "closing")
    ]
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    statuses = {p["product_id"]: p for p in snapshot["products"]}
    assert snapshot["outcome"] == "partial"
    assert statuses["P2"]["status"] == "done" and statuses["P1"]["status"] == "failed"
    assert any(i["unit_index"] == 4 for i in statuses["P1"]["issues"])
    with pytest.raises(ScriptAuthoringError):
        await service.approve_product(
            set_id=set_id,
            product_id="P1",
            version_id=version.id,
            actor="owner",
            is_human=True,
            authorized=True,
        )


@pytest.mark.asyncio
async def test_provider_errors_are_bounded_and_never_crash_the_batch(monkeypatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    llm = FakeLLM(raises=LLMClientError("boom"))
    service, repos = _service(llm)
    set_id = await _new_set(service)
    batch_id = await _run(service, repos, set_id)
    snapshot = await service.get_batch(set_id=set_id, batch_id=batch_id)
    assert snapshot["outcome"] == "failed"
    item, version = await _text(repos, set_id, "P2")
    assert item.state is ScriptState.GATE_FAILED
    assert all(u.startswith("<Phần này chưa soạn được") for u in split_units(version.spoken_text))
    # <= 2 transport attempts per call, 2 calls per unit (empty-output retry never triggers on errors)
    assert len(llm.prompts) <= 14 * 2


@pytest.mark.asyncio
async def test_existing_owner_text_is_never_overwritten() -> None:
    llm = FakeLLM()
    service, repos = _service(llm)
    set_id = await _new_set(service)
    await service.save_draft(
        set_id=set_id,
        product_id="P2",
        display_text="Bài của chủ shop.",
        spoken_text="Bài của chủ shop.",
        revision=None,
    )
    await _run(service, repos, set_id)
    item, version = await _text(repos, set_id, "P2")
    assert version.spoken_text == "Bài của chủ shop."
    assert not any("Tên sản phẩm: Kem ABC" in p for p in llm.prompts)
    assert (await _text(repos, set_id, "P1"))[0].state is ScriptState.REVIEWABLE


@pytest.mark.asyncio
async def test_idempotency_key_rejects_a_different_request_and_foreign_batches() -> None:
    service, repos = _service(FakeLLM())
    set_id = await _new_set(service)
    other_set = await _new_set(service)
    batch_id = await _run(service, repos, set_id, key="same")
    again = await service.start_batch_generation(
        set_id=set_id,
        product_ids=[],
        target_duration_s=600,
        idempotency_key="same",
        ordered_units=True,
    )
    assert again["batch_id"] == batch_id and again["idempotent"] is True
    with pytest.raises(ScriptAuthoringError) as conflict:
        await service.start_batch_generation(
            set_id=set_id,
            product_ids=["P1"],
            target_duration_s=600,
            idempotency_key="same",
            ordered_units=True,
        )
    assert conflict.value.code == "idempotency_conflict"
    for call in (service.get_batch, service.cancel_batch):
        with pytest.raises(ScriptAuthoringError) as foreign:
            await call(set_id=other_set, batch_id=batch_id)
        assert foreign.value.code == "not_found"
    assert await service.get_batch_events_snapshot(set_id=other_set, batch_id=batch_id) is None


def test_roles_are_fixed_by_position_and_claims() -> None:
    assert plan_roles(first=True, last=False, claim_count=2) == [
        "opening",
        "intro",
        "benefit",
        "benefit",
        "offer",
        "trust",
        "cta",
    ]
    assert plan_roles(first=False, last=True, claim_count=5) == [
        "intro",
        "benefit",
        "benefit",
        "benefit",
        "offer",
        "trust",
        "cta",
        "closing",
    ]
    assert plan_roles(first=True, last=True, claim_count=0).count("benefit") == 1


def test_clean_unit_text_rejects_garbage_and_normalises_style() -> None:
    assert clean_unit_text("") is None
    assert clean_unit_text("...") is None
    assert clean_unit_text("x" * 800) is None
    assert clean_unit_text('"Giá tốt — chốt đơn nhé"') == "Giá tốt, chốt đơn nhé"
    assert clean_unit_text("- Một ý.\n\nVà ý nữa.") == "Một ý. Và ý nữa."


def test_bounded_llm_retries_once_then_stops_and_honours_deadline() -> None:
    calls = []

    def flaky(prompt):
        calls.append(prompt)
        raise ConnectionError("down")

    bounded = BoundedLLM(flaky, attempts=2, sleep=lambda _s: None)
    with pytest.raises(ConnectionError):
        bounded("p")
    assert len(calls) == 2

    now = [0.0]
    late = BoundedLLM(flaky, job_deadline_s=10, clock=lambda: now[0], sleep=lambda _s: None)
    now[0] = 11.0
    with pytest.raises(LLMDeadlineError):
        late("p")
    assert len(calls) == 2  # the expired job made no further call


def test_short_one_sentence_units_pass_role_bounds_and_common_phrases_only_warn() -> None:
    from backend.application.script_authoring.gate.context import ProductFacts, ScriptGateContext

    gate = ScriptAuthoringServiceImpl._default_gate()
    low, high = __import__(
        "backend.application.script_authoring.generation.ordered_units", fromlist=["x"]
    ).role_bounds_s("cta")
    ctx = ScriptGateContext(facts=ProductFacts(), target_min_seconds=low, target_max_seconds=high)
    assert gate.run_segment("Chốt đơn nhé cả nhà ơi.", ctx).passed
    # a phrase shared by 6 of 7 units is a warning, not a block
    units = [f"Cả nhà ơi mình nói ý số {w} nhé." for w in "một hai ba bốn năm sáu bảy".split()]
    full = gate.run_full_script(units, ScriptGateContext(facts=ProductFacts(), total_min_seconds=1))
    assert full.passed
