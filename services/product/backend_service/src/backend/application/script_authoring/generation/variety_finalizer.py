"""Lease-fenced, best-effort style repair before an ordered batch is completed."""

from __future__ import annotations

from dataclasses import replace

from ..compile import apply_brand_pronunciation
from ..gate.context import ScriptGateContext
from ..models import GateRun, ScriptSegment, ScriptSource, ScriptState, ScriptVersion, new_id
from ..repositories import LeaseLostError
from .ordered_units import (
    brand_mentions,
    build_unit_repair_prompt,
    check_unit_text,
    role_bounds_s,
    spoken_price,
)
from .script_variety import (
    VarietyPart,
    audit,
    improves,
    opening_only,
    repair_budget,
    repair_candidates,
)
from .unit_brief import facts_revision, find_brief, provenance, recap_violation, spec_from_brief


async def selected_parts(service, script_set):
    """Include selected versions of every product, including skipped siblings."""
    items = {i.product_id: i for i in await service._repos.items.list_by_set(script_set.id)}
    parts, records = [], {}
    for pid in script_set.product_ids:
        facts = service._product_facts(script_set.brief, pid)
        authoritative = (
            *facts.allowed_claims,
            *facts.discounts,
            facts.product_name,
            *(spoken_price(price) for price in facts.prices),
        )
        item = items.get(pid)
        if item is None or not item.current_version_id:
            continue
        version = await service._repos.versions.get(item.current_version_id)
        if version is None:
            continue
        selected = [await service._repos.segments.get(sid) for sid in version.segment_version_ids]
        if not selected or any(s is None for s in selected):
            # Legacy/manual whole scripts remain visible to the detector too.
            if version.spoken_text:
                parts.append(VarietyPart(f"legacy:{item.id}", pid, "", version.spoken_text))
            continue
        for segment in selected:
            plan = await service._repos.plans.get(segment.plan_id)
            brief = find_brief(plan.unit_briefs, segment.part_id) if plan else None
            key = f"{pid}:{segment.part_id or segment.id}"
            editable = (
                bool(brief and segment.part_id)
                and (segment.provenance or {}).get("source") == "llm"
                and item.state in {ScriptState.DRAFT, ScriptState.REVIEWABLE}
            )
            parts.append(
                VarietyPart(
                    key,
                    pid,
                    brief["role"] if brief else segment.intent,
                    segment.spoken_text,
                    editable,
                    tuple(
                        dict.fromkeys(
                            (
                                *authoritative,
                                *(
                                    f["text"]
                                    for f in (brief or {}).get("required_facts", ())
                                    if f.get("must_keep")
                                ),
                            )
                        )
                    ),
                )
            )
            records[key] = (item, version, selected, segment, plan, brief)
    return parts, records


class VarietyCancelled(Exception):
    pass


async def checkpoint(service, state, lease):
    if lease is None:
        raise LeaseLostError("variety finalization requires a captured lease")
    result = await service._repos.batches.get(state.batch_id)
    if result is None:
        raise RuntimeError("missing batch during variety finalization")
    if result[0].cancel_requested:
        raise VarietyCancelled()
    current = result[1]
    next_state = state.model_copy(deep=True)
    next_state.revision = current.revision + 1
    await service._repos.batches.update_state(
        state.batch_id,
        state=next_state,
        expected_revision=current.revision,
        lease_owner=lease[0],
        lease_epoch=lease[1],
        lease_duration_s=service._config.recovery_lease_seconds,
    )
    state.revision = next_state.revision


async def finalize(service, state, bridge, revisions, existing_versions, *, lease=None):
    """At most one reserved model attempt per stable part, bounded across recovery.

    Style failures retain the already valid text. Persistence/lease failures
    propagate: a worker must never declare completion after losing its fence.
    """
    from ..service_impl import _PRODUCT_SCRIPT_MIN_SECONDS, _brand_allowlist, _to_gate_violation

    if not state.ordered_units or state.variety.get("done"):
        return
    lease = lease or service._batch_lease(state.batch_id)
    if lease is None:
        raise LeaseLostError("variety finalization requires an owned batch")
    await service._drain_artifacts(
        bridge,
        revisions,
        existing_versions,
        batch_id=state.batch_id,
        batch_lease=lease,
    )
    script_set = await service._repos.script_sets.get(state.script_set_id)
    if script_set is None:
        raise RuntimeError("missing script set during variety finalization")
    # Skipped/pre-existing products are audit-only: a new batch must never
    # reset their original write budget or mutate saved owner content.
    eligible = {
        f"{pid}:{part_id}"
        for pid, product_state in state.products.items()
        if product_state.status == "completed"
        and not product_state.workflow_snapshot.get("replacement_failed")
        for part_id in product_state.workflow_snapshot.get("part_ids", ())
    }

    def editable_in_this_batch(parts):
        return [replace(p, editable=p.editable and p.key in eligible) for p in parts]

    parts, _ = await selected_parts(service, script_set)
    parts = editable_in_this_batch(parts)
    report = audit(parts)
    state.variety.setdefault("attempted", [])
    state.variety.setdefault("calls", 0)
    state.variety.setdefault("initial_excess", report.excess)
    state.variety.setdefault("limit", repair_budget(len(parts)))
    await checkpoint(service, state, lease)
    budget = getattr(service._repos, "model_budget", None)
    ordered = service._ordered_batch(
        script_set,
        service._safe_llm(),
        started_at=state.started_at,
        target_duration_s=next(iter(state.target_durations.values()), 0),
    )
    for candidate in repair_candidates(parts, report):
        if len(state.variety["attempted"]) >= state.variety["limit"]:
            break
        if candidate.key in state.variety["attempted"] or budget is None:
            continue
        if await service._batch_cancel_requested(state.batch_id):
            raise VarietyCancelled()
        # Re-read selected versions before each call, so neither an intervening
        # owner edit nor an earlier repair can be overwritten by a stale copy.
        parts, records = await selected_parts(service, script_set)
        parts = editable_in_this_batch(parts)
        fresh = next((p for p in parts if p.key == candidate.key), None)
        if (
            fresh is None
            or not fresh.editable
            or fresh not in repair_candidates(parts, audit(parts))
        ):
            continue
        item, source, selected, current, plan, brief = records[candidate.key]
        expected_item_revision = item.revision
        product = ordered.products[item.product_id]
        if brief.get("facts_revision") != facts_revision(product):
            continue
        roles = [spec_from_brief(b) for b in plan.unit_briefs["parts"]]
        index = next(
            i for i, b in enumerate(plan.unit_briefs["parts"]) if b["part_id"] == current.part_id
        )
        spec = roles[index]
        state.variety["attempted"].append(candidate.key)
        await checkpoint(
            service, state, lease
        )  # BEFORE reservation/dispatch, survives ambiguous calls
        if not await budget.reserve(state.batch_id, item.product_id, current.segment_index):
            continue  # keep the original three-write budget for this part
        state.variety["calls"] += 1
        state.actual_semantic_calls = (
            sum(p.semantic_calls for p in state.products.values()) + state.variety["calls"]
        )
        await checkpoint(service, state, lease)
        prompt = build_unit_repair_prompt(
            ordered.session,
            product,
            roles,
            index,
            # What the model wrote: spoken_text may carry a brand pronunciation it must not copy.
            failed_text=current.display_text,
            problems=[
                "Chỉ đổi tối đa sáu từ dẫn trung tính ở đầu câu bị lặp, giữ nguyên số câu và "
                "toàn bộ phần còn lại, mọi giá, thông tin và điều kiện bắt buộc. "
                "Ví dụ 'Giá chỉ' có thể đổi thành 'Mức giá là'; không thêm nhận định mới."
            ],
            used_openings=tuple(
                dict.fromkeys(" ".join(p.text.split()[:5]) for p in parts if p.key != fresh.key)
            )[:24],
        )
        try:
            raw = await service._with_lease_heartbeat(
                lambda: ordered.llm(prompt, repair=True), batch_id=state.batch_id, batch_lease=lease
            )
        except LeaseLostError:
            raise
        except Exception:
            continue  # style never triggers a fallback or another repair attempt
        if await service._batch_cancel_requested(state.batch_id):
            raise VarietyCancelled()
        # Both snapshot and selection must still be current after the provider wait.
        latest = await service._repos.script_sets.get(script_set.id)
        if latest is None or latest.revision != script_set.revision:
            continue
        current_item = await service._repos.items.get(item.id)
        if (
            current_item is None
            or current_item.revision != expected_item_revision
            or current_item.current_version_id != source.id
        ):
            continue
        latest_parts, _ = await selected_parts(service, latest)
        position = next(i for i, s in enumerate(selected) if s.id == current.id)
        text, _reason = check_unit_text(
            raw,
            spec=spec,
            prices=product.prices,
            allow_bridge=spec.bridge,
            approved=(*product.claims, *product.discounts),
            product_name=product.name,
            name_used_elsewhere=sum(
                product.name.lower() in s.display_text.lower()
                for s in selected
                if s.id != current.id
            )
            if product.name
            else 0,
            must=spec.must,
            aware=script_set.brief.transition_policy == "ORDER_AWARE",
            brand=product.brand,
            brand_used_nearby=sum(
                brand_mentions(selected[p].display_text, product.brand, product.name) > 0
                for p in (position - 1, position + 1)
                if 0 <= p < len(selected)
            ),
        )
        if (
            text is None
            or not opening_only(current.display_text, text)
            or any(
                spoken_price(price) in current.display_text and spoken_price(price) not in text
                for price in product.prices
            )
            or (spec.revisit and recap_violation(text))
            or not improves(latest_parts, fresh.key, text)
        ):
            continue
        facts = service._product_facts(script_set.brief, item.product_id)
        allowlist = _brand_allowlist(script_set.brief, item.product_id)
        spoken = apply_brand_pronunciation(text, product.brand, product.brand_pronunciation)
        low, high = role_bounds_s(spec.role)
        context = ScriptGateContext(
            facts=facts,
            brand_allowlist=allowlist,
            transition_policy=script_set.brief.transition_policy,
            target_min_seconds=low,
            target_max_seconds=high,
        )
        if not service._gate.run_segment(spoken, context).passed:
            continue
        candidate_texts = [spoken if s.id == current.id else s.spoken_text for s in selected]
        result = service._gate.run_full_script(
            candidate_texts,
            ScriptGateContext(
                facts=facts,
                brand_allowlist=allowlist,
                transition_policy=script_set.brief.transition_policy,
                total_min_seconds=_PRODUCT_SCRIPT_MIN_SECONDS,
            ),
        )
        if not result.passed:
            continue
        rows = await service._repos.segments.list_by_plan(plan.id)
        segment = ScriptSegment(
            **{
                **current.model_dump(exclude={"created_at"}),
                "id": new_id("segment"),
                "version": max(s.version for s in rows if s.segment_index == current.segment_index)
                + 1,
                "display_text": text,
                "spoken_text": spoken,
                "status": ScriptState.DRAFT,
                "provenance": {**provenance("llm"), "variety_repair": True},
            }
        )
        selected = [segment if s.id == current.id else s for s in selected]
        gate = GateRun(
            id=new_id("gate_run"),
            script_item_id=item.id,
            full=True,
            passed=True,
            violations=[_to_gate_violation(v) for v in result.violations],
        )
        versions = await service._repos.versions.list_by_item(item.id)
        version = ScriptVersion(
            id=new_id("script_version"),
            script_item_id=item.id,
            version=max(v.version for v in versions) + 1,
            state=ScriptState.REVIEWABLE,
            source=ScriptSource.AI_REGENERATE,
            display_text="\n\n".join(s.display_text for s in selected),
            spoken_text="\n\n".join(s.spoken_text for s in selected),
            segment_version_ids=[s.id for s in selected],
            gate_run_id=gate.id,
            plan_version=source.plan_version,
        )
        item = item.model_copy(deep=True)
        revisions[item.id] = item.revision
        item.current_version_id, item.state = version.id, ScriptState.REVIEWABLE
        for artifact in (segment, gate, version, item):
            bridge(artifact)
        await service._drain_artifacts(
            bridge,
            revisions,
            existing_versions,
            batch_id=state.batch_id,
            batch_lease=lease,
        )
        parts = [replace(p, text=text) if p.key == fresh.key else p for p in parts]
        report = audit(parts)
    # A remaining style warning is informative, never a content/fact gate failure.
    parts, _ = await selected_parts(service, script_set)
    report = audit(parts)
    state.variety.update(
        done=True, remaining_excess=report.excess, warning_parts=len(report.sentence_warnings)
    )
    await checkpoint(service, state, lease)
