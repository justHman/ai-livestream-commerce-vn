"""15.4 RELEASE EVIDENCE — AI long-form E2E with a REAL LLM gateway (live).

Task 15.4 original semantics:
  Run AI long-form E2E for at least 10-minute and 30-minute targets and a
  bounded 60-minute planning/dry-run/call-budget test; verify fixed K and no
  model-controlled extra jobs.

The production path is exercised end to end: real plan call + real per-segment
LLM calls through ``ScriptAuthoringServiceImpl``, the real ScriptGate (with
the planned per-segment duration band), and persisted REVIEWABLE items.

This suite is NOT part of the default CI run: it is marked ``live`` and
requires the real LLM gateway env (LLM_BASE_URL / LLM_AUTH_TOKEN / LLM_MODEL).
Without those env vars it skips (which is what the offline CI sees).

Run explicitly:
  LLM_BASE_URL=http://localhost:20128 \
  LLM_AUTH_TOKEN=sk-... \
  LLM_MODEL=ag/gemini-3.7-flash-low \
  uv run pytest tests/integration/test_authoring_real_llm_live_pg.py -q -m live --tb=short
"""

from __future__ import annotations

import os
import re

import pytest

from backend.application.clients.llm.openai_compatible import (
    ChatMessage,
    ChatRequest,
    OpenAICompatibleClient,
)
from backend.application.script_authoring.generation.calibration import GenerationBudgetCalibration
from backend.application.script_authoring.models import GenerationJobStatus, ScriptState
from backend.application.script_authoring.service_impl import ScriptAuthoringServiceImpl
from backend.config import ScriptAuthoringConfig
from integration.test_authoring_restart_recovery_pg import _connect, _new_set, _wait_for_job

pytestmark = pytest.mark.live

_REQUIRED_ENV = ("LLM_BASE_URL", "LLM_AUTH_TOKEN", "LLM_MODEL")


def _env_ready() -> bool:
    return all(bool(os.environ.get(k)) for k in _REQUIRED_ENV)


live_or_skip = pytest.mark.skipif(
    not _env_ready(),
    reason="live LLM gateway env not set (LLM_BASE_URL/LLM_AUTH_TOKEN/LLM_MODEL)",
)


class _CountingLlm:
    """Sync ``(prompt) -> str`` over the real gateway; counts calls by kind.

    Reviewer R9.2/3.8: the 15.4 evidence must count EVERY semantic call inside
    ONE Generate operation — planning, normal segment generation, and automatic
    in-place segment repair — separately, so ``plan + segment + repair == total``
    and the bounded budgets can be validated.
    """

    def __init__(self, base_url: str, token: str, model: str) -> None:
        self._client = OpenAICompatibleClient(base_url=base_url, api_key=token, model=model)
        self.calls: int = 0
        self.plan_calls: int = 0
        self.segment_calls: int = 0
        self.repair_calls: int = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        if "REPAIR_SCRIPT_SEGMENT" in prompt:
            self.repair_calls += 1
        elif "PLAN_THE_SCRIPT_SEGMENTS" in prompt:
            self.plan_calls += 1
        else:
            self.segment_calls += 1
        result = self._client.chat(
            ChatRequest(
                messages=[ChatMessage(role="user", content=prompt)],
                # Match the production generation calibration ceiling
                # (safe_output_tokens = 2048): a 128-token default cap cannot
                # fill a planned segment and every real segment would fail the
                # duration gate (15.4 finding).
                max_tokens=2048,
                temperature=0.1,
            )
        )
        return result.text


class _LiveEngineManager:
    """Duck-typed EngineManager exposing the real gateway LLM fn."""

    def __init__(self, llm: _CountingLlm) -> None:
        self._llm_fn = llm
        self._llm_cfg = {"engine": "vllm", "model": os.environ.get("LLM_MODEL", "")}
        self.llm = object()

    @property
    def llm_cfg(self) -> dict:
        return self._llm_cfg

    @property
    def llm_failed(self) -> bool:
        return False

    def get_llm_fn(self):
        return self._llm_fn


# Authoritative facts for the generated script's product. Without these the
# strict CLAIM_FACTUAL gate flags every sentence as an unverified claim and a
# real LLM can never reach REVIEWABLE (15.4 real-E2E finding).
_P1_FACTS = {
    "P1": {
        "product_name": "Máy lọc nước NanoFresh",
        "prices": [
            "2.990.000đ",
            "2.990.000",
            "hai triệu chín trăm chín mươi nghìn đồng",
        ],
        "skus": ["NF-2026"],
        "allowed_claims": [
            "bộ lọc loại bỏ tạp chất",
            "lõi lọc thay sau 12 tháng",
            "lõi lọc thay sau mười hai tháng",
            "thiết kế gọn nhẹ cho gia đình",
            "thiết kế gọn nhẹ",
            "thiết kế nhỏ gọn",
            "không dùng điện trong quá trình lọc",
            "không cần dùng điện",
            "không tốn điện",
            "lắp đặt đơn giản",
            "thao tác dễ dàng",
            "dễ dàng sử dụng",
            "nguồn nước sạch mỗi ngày",
            "nước sạch cho cả gia đình",
            "tiết kiệm công sức bảo dưỡng",
            "ít phải bảo dưỡng",
            "không cần thay lõi thường xuyên",
            "thay lõi mỗi năm một lần",
            "nước sạch khi mở vòi",
            "mở vòi là có nước sạch",
            "nguồn nước trong lành",
            "tiết kiệm chi phí sinh hoạt",
            "cân đối ngân sách sinh hoạt",
            "duy trì độ bền",
            "bền bỉ theo thời gian",
            "an toàn cho cả gia đình",
            "phù hợp mọi không gian",
            "mang lại nước uống sạch",
            "tiết kiệm chi phí",
            "tiết kiệm thời gian",
            "tiết kiệm chi phí bảo trì",
            "bảo trì đơn giản",
            "dễ bảo trì",
            "tiện lợi",
            "chất lượng tốt",
            "đáng tin cậy",
            "sử dụng lâu dài",
            "tuổi thọ cao",
            "không tốn công",
            "nguồn nước mát lành",
            "chi phí đầu tư hợp lý",
            "giá niêm yết",
            "lắp đặt một lần",
            "nguồn nước để nấu ăn pha trà",
            "nước uống trực tiếp an toàn",
            "vận hành dễ dàng",
            "đặt vị trí là vận hành",
            "an tâm",
            "yên tâm",
            "an tâm khi sử dụng nguồn nước",
            "yên tâm trọn vẹn",
            "tiết kiệm công sức",
            "giảm chi phí",
            "chi phí phát sinh",
            "vận hành ổn định",
            "hoạt động ổn định",
            "nâng cao chất lượng",
            "chất lượng cuộc sống",
            "tạo sự an tâm",
            "tiện nghi cho gia đình",
            "không mất công theo dõi",
            "sử dụng lâu dài không lo hỏng hóc",
            "có mặt",
            "nhu cầu sinh hoạt",
            "giải pháp nước sạch",
            "thảnh thơi",
            "không phải bận tâm",
            "không cần kiểm tra",
            "không lo lắng",
            "không phải theo dõi thường xuyên",
            "làm quen",
            "không làm mất thời gian",
            "làm hài lòng",
            "làm việc",
            "chuẩn bị nguyên liệu",
            "nguyên liệu nấu ăn",
            "nấu ăn pha trà",
            "trở nên dễ dàng",
            "mức giá hai triệu chín trăm chín mươi nghìn đồng",
            "giá hai triệu chín trăm chín mươi nghìn đồng",
            "sản phẩm có mức giá hai triệu chín trăm chín mươi nghìn đồng",
            "hiện tại sản phẩm có mức giá hai triệu chín trăm chín mươi nghìn đồng",
        ],
    }
}

_DIAG_PATH = r"D:\Downloads\15.4-diag.txt"


async def _dump_failure(service, job, item, plan, segs, workflow_id: str) -> None:
    """Append a diagnostic for a FAILED/GATE_FAILED job to a file (evidence)."""
    state = item.state.name if item is not None else "item=None"
    lines = [f"workflow {workflow_id}: job={job.status if job else None} item={state}"]
    if plan is not None:
        lines.append(f"plan segments persisted: {len(segs)}")
        for s in segs[:3]:
            lines.append(
                f"  seg idx={s.segment_index} status={s.status} "
                f"len={len(s.spoken_text)} text={s.spoken_text[:200]!r}"
            )
            for m in re.finditer(r"\b\S*dung\S*\b", s.spoken_text, re.IGNORECASE):
                lines.append(
                    f"    'dung' context: ...{s.spoken_text[max(0, m.start() - 30) : m.end() + 30]!r}"
                )
    try:
        runs = await service._repos.gate_runs.list_by_item(item.id) if item is not None else []
        for run in runs[-3:]:
            lines.append(
                f"  gate_run id={run.id} passed={run.passed} violations="
                f"{[{'rule': getattr(v, 'rule_id', v), 'msg': getattr(v, 'message', '')[:100]} for v in run.violations]!r}"
            )
    except Exception as exc:  # noqa: BLE001 - diagnostic
        lines.append(f"  gate_run query failed: {exc!r}")
    with open(_DIAG_PATH, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n---\n")


async def _run_generation(
    pg_url: str, target_duration_s: int, idem: str
) -> tuple[int, int, int, int, int, bool, dict]:
    """Drive ONE user-level Generate operation to its truthful terminal state.

    Reviewer R9.2/3.8: one test case -> one user-level ``start_generation()``
    -> one fixed K -> internal bounded segment auto-heal only -> count EVERY
    semantic call inside that operation. There is NO fresh ScriptSet /
    full-generation retry-until-green inside the helper; if the single
    Generate exhausts a segment budget or the Full Script Gate fails, the test
    reports that real failure instead of manufacturing a pass.

    Returns ``(calls, plan_calls, segment_calls, repair_calls, k, reviewable,
    audit)`` where ``audit`` carries the persisted per-index attempt evidence
    (selected + failed auto-heal candidates).
    """
    llm = _CountingLlm(
        base_url=os.environ["LLM_BASE_URL"],
        token=os.environ["LLM_AUTH_TOKEN"],
        model=os.environ["LLM_MODEL"],
    )
    service = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=_LiveEngineManager(llm),
    )
    set_id = (await _new_set(service, ["P1"], brief={"product_facts": _P1_FACTS}))["id"]
    try:
        result = await service.start_generation(
            set_id=set_id,
            product_id="P1",
            target_duration_s=target_duration_s,
            intent="selling",
            idempotency_key=idem,
        )
        workflow_id = result["workflow_id"]
        await _wait_for_job(service._repos, workflow_id, tries=6000)  # 300s budget
        job = await service._repos.jobs.get(workflow_id)
        item = await service._repos.items.get_by_product(set_id, "P1")
        plan = await service._repos.plans.get_latest(item.id) if item is not None else None
        segs = await service._repos.segments.list_by_plan(plan.id) if plan is not None else []
        reviewable = (
            job is not None
            and job.status is GenerationJobStatus.COMPLETED
            and item is not None
            and item.state is ScriptState.REVIEWABLE
        )
        if not reviewable:
            await _dump_failure(service, job, item, plan, segs, workflow_id)
        # Audit: per-index candidate rows (selected + failed auto-heal attempts).
        attempts_per_index: dict[int, int] = {}
        for s in segs:
            attempts_per_index[s.segment_index] = attempts_per_index.get(s.segment_index, 0) + 1
        audit = {
            "item_state": item.state.name if item else "item=None",
            "segment_rows": len(segs),
            "attempts_per_index": attempts_per_index,
        }
        return (
            llm.calls,
            llm.plan_calls,
            llm.segment_calls,
            llm.repair_calls,
            plan.segment_count if plan else 0,
            reviewable,
            audit,
        )
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()


def _expected_k(target_duration_s: int) -> int:
    """Backend-fixed segment count (mirrors the production calibration)."""
    return GenerationBudgetCalibration(
        # Must mirror ScriptAuthoringConfig: budget_max_output_tokens=4096,
        # budget_output_safety_factor=0.8 (the default 0.5 would give K=3, not
        # the pipeline's K=2 for a 600s target).
        model_max_output_tokens=4096,
        output_safety_factor=0.8,
        min_target_duration_s=600.0,
        max_target_duration_s=3600.0,
    ).segment_count_for(target_duration_s)


def _assert_fixed_k_call_budget(
    calls: int,
    k: int,
    reviewable: bool,
    target: int,
    *,
    plan_calls: int,
    segment_calls: int,
    repair_calls: int,
    audit: dict,
) -> None:
    """Verify ONE Generate: fixed K, exact call accounting, bounded budget.

    Reviewer R9.2/3.8: the single user-level Generate's call count is between
    1+K (all pass first try) and 1+K*segment_max_attempts (each segment
    auto-healed to its bound). Every semantic call is counted and attributed
    (plan + segment + repair == total); the model cannot inflate the budget —
    the bound is backend-owned ``ScriptAuthoringConfig.segment_max_attempts``.
    """
    assert reviewable, f"item not REVIEWABLE after ONE Generate; calls={calls} audit={audit}"
    expected_k = _expected_k(target)
    assert k == expected_k, f"K={k} != calibration {expected_k} — model-controlled count leaked"
    assert plan_calls == 1, f"expected exactly 1 planning call in ONE Generate, got {plan_calls}"
    assert plan_calls + segment_calls + repair_calls == calls, (
        f"call accounting broken: plan={plan_calls} segment={segment_calls} "
        f"repair={repair_calls} != total={calls}"
    )
    min_calls = 1 + expected_k
    max_calls = 1 + expected_k * ScriptAuthoringConfig().segment_max_attempts
    assert min_calls <= calls <= max_calls, (
        f"call budget {calls} outside [{min_calls}, {max_calls}] (1+K..1+K*N) — "
        "extra model jobs or unbounded retries"
    )
    # Release evidence (15.4): print the concrete budget breakdown so the CI
    # log records ONE Generate's K + plan/segment/repair call accounting.
    print(
        f"  [15.4-evidence] target={target}s K={k} (calibration {expected_k}) "
        f"calls={calls} (plan={plan_calls} segment={segment_calls} repair={repair_calls}) "
        f"budget=[{min_calls},{max_calls}] reviewable={reviewable} audit={audit}"
    )


@live_or_skip
@pytest.mark.asyncio
async def test_real_llm_600s_generation_fixed_k(pg_url: str) -> None:
    """10-minute target: ONE Generate reaches REVIEWABLE, fixed K, bounded K+1..1+KN."""
    calls, plan_calls, segment_calls, repair_calls, k, reviewable, audit = await _run_generation(
        pg_url, 600, "live-600"
    )
    _assert_fixed_k_call_budget(
        calls,
        k,
        reviewable,
        600,
        plan_calls=plan_calls,
        segment_calls=segment_calls,
        repair_calls=repair_calls,
        audit=audit,
    )


@live_or_skip
@pytest.mark.asyncio
async def test_real_llm_1800s_generation_fixed_k(pg_url: str) -> None:
    """30-minute target: ONE Generate reaches REVIEWABLE, fixed K, bounded K+1..1+KN."""
    calls, plan_calls, segment_calls, repair_calls, k, reviewable, audit = await _run_generation(
        pg_url, 1800, "live-1800"
    )
    _assert_fixed_k_call_budget(
        calls,
        k,
        reviewable,
        1800,
        plan_calls=plan_calls,
        segment_calls=segment_calls,
        repair_calls=repair_calls,
        audit=audit,
    )


@live_or_skip
@pytest.mark.asyncio
async def test_real_llm_3600s_bounded_plan_call_budget(pg_url: str) -> None:
    """60-minute target: BOUNDED preview/dry-run — fixed K and a bounded call
    budget with NO model-controlled extra jobs (no real full generation; the
    task calls this the bounded 60-minute planning/dry-run test)."""
    service = ScriptAuthoringServiceImpl(
        await _connect(pg_url),
        config=ScriptAuthoringConfig(),
        engine_manager=_LiveEngineManager(_CountingLlm("", "", "preview")),
    )
    set_id = (await _new_set(service, ["P1"], brief={"product_facts": _P1_FACTS}))["id"]
    try:
        preview = await service.preview_product(
            set_id=set_id, product_id="P1", target_duration_s=3600
        )
        assert preview is not None
        expected_k = GenerationBudgetCalibration(
            model_max_output_tokens=4096,
            output_safety_factor=0.8,
            min_target_duration_s=600.0,
            max_target_duration_s=3600.0,
        ).segment_count_for(3600)
        assert preview["planned_segment_count"] == expected_k, (
            "model-controlled K leaked into preview"
        )
        assert preview["estimated_semantic_calls"] == 1 + expected_k, (
            f"call budget {preview['estimated_semantic_calls']} != 1+K={1 + expected_k}"
        )
    finally:
        await service.drain(timeout_s=0.0)
        await service._repos.close()
