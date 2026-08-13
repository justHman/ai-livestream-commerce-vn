"""Tasks 6.1-6.7 tests: context/intent/prompt builders, contracts, budgets.

Covers:

- 6.1  build_authoritative_context selects ONLY required slices from a
       versioned authoritative-context dict and rejects missing keys.
- 6.2  ScriptIntent/TransitionContext: ORDER_AGNOSTIC strips adjacent-product
       summaries; ORDER_AWARE keeps deterministic summaries.
- 6.3  build_generate_prompt = skill + constraints + authoritative context +
       duration/intent + plan/segment assignment (+ continuity).
- 6.4  build_repair_prompt = immutable source + exact failed rules + only
       anti-drift facts; forbids broad rewrite/new claim/new CTA.
- 6.5  Prompt-contract tests: Fix excludes sales skill/unrelated rules;
       Generate excludes repair-only instructions.
- 6.6  Contract tests: prompt/context modules expose no tools/function-
       calling, no filesystem/web/job-management/product-traversal tools, no
       model-controlled iteration API (source grep over the modules).
- 6.7  estimate_tokens (chars/4) + guard_budget raising OversizedContextError
       instead of silently truncating.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from backend.application.entity.models import EntityDocument, Fact, Relation
from backend.application.script_authoring.generation.context_builder import (
    AuthoritativeContext,
    MissingContextKeyError,
    build_authoritative_context,
    build_authoritative_context_from_entity,
)
from backend.application.script_authoring.generation.continuity import (
    ContinuityState,
    build_tail,
)
from backend.application.script_authoring.generation.intent import (
    ScriptIntent,
    TransitionContext,
    build_transition_context,
    transition_guidance,
)
from backend.application.script_authoring.generation.prompt_builder import (
    OversizedContextError,
    PromptBuildError,
    PromptParts,
    build_generate_prompt,
    build_repair_prompt,
    estimate_tokens,
    guard_budget,
)

# ---------------------------------------------------------------------------
# Fixtures (task 6.1 authoritative-context dict; task 6.3/6.4 builders)


def _full_context_dict() -> dict:
    return {
        "shop": {"name": "LIVENTO", "region": "Hà Nội"},
        "persona": {"name": "Minh", "tone": "thân thiện"},
        "campaign": {"id": "CAM-2026-08", "title": "Back to school"},
        "product": {
            "id": "P001",
            "name": "Kem ABC",
            "price": "299.000đ",
            "sku": "KEM-ABC-01",
        },
        "promotions": [
            {"id": "PROMO-1", "text": "Giảm 20%"},
            {"id": "PROMO-2", "text": "Freeship 50k"},
        ],
        "facts": [{"id": "FACT-1", "text": "Kem ABC chống nắng SPF 50"}],
    }


def _build_generate(transition: TransitionContext) -> PromptParts:
    return build_generate_prompt(
        skill_text="SKILL_MARKER_PRESENT",
        generation_constraints=["CLAIM_PRICE: only claim the authoritative price"],
        context=build_authoritative_context(_full_context_dict()),
        duration_s=600,
        intent=ScriptIntent(intent="FEATURE_BENEFIT", target_duration_s=600),
        transition=transition,
        plan={"segments": 3, "topics": "a;b;c"},
        segment_index=1,
        continuity=ContinuityState(
            previous_segment_tail="...", covered_fact_ids=frozenset({"FACT-1"})
        ),
    )


def _build_repair() -> PromptParts:
    return build_repair_prompt(
        source_text="Kem ABC giá 100.000đ, giảm 50%.",
        failed_rule_ids=["CLAIM_PRICE", "CLAIM_DISCOUNT"],
        rule_repair_instructions=[
            "Use only the authoritative price.",
            "Use only the authoritative discount.",
        ],
        authoritative_facts=build_authoritative_context(
            {
                "shop": {"name": "LIVENTO"},
                "persona": {},
                "campaign": {},
                "product": {"id": "P001", "name": "Kem ABC", "price": "299.000đ"},
                "promotions": [{"id": "PROMO-1", "text": "Giảm 20%"}],
                "facts": [],
            }
        ),
    )


# ---------------------------------------------------------------------------
# 6.1 authoritative context builder


class TestContextBuilder:
    def test_selects_only_required_slices(self) -> None:
        source = _full_context_dict()
        source["unrelated"] = {"secret": "catalog-data"}
        ctx = build_authoritative_context(source)

        assert isinstance(ctx, AuthoritativeContext)
        assert ctx.shop["name"] == "LIVENTO"
        assert ctx.persona["name"] == "Minh"
        assert ctx.campaign["id"] == "CAM-2026-08"
        assert ctx.product["price"] == "299.000đ"
        assert ctx.promotions[0]["id"] == "PROMO-1"
        assert ctx.facts[0]["id"] == "FACT-1"
        # Unrelated catalog data is never selected into the prompt model.
        assert not hasattr(ctx, "unrelated")

    @pytest.mark.parametrize(
        "missing_key",
        ["shop", "persona", "campaign", "product"],
    )
    def test_missing_required_key_raises(self, missing_key: str) -> None:
        source = _full_context_dict()
        del source[missing_key]
        with pytest.raises(MissingContextKeyError):
            build_authoritative_context(source)

    def test_wrong_section_shape_raises(self) -> None:
        source = _full_context_dict()
        source["product"] = "not-a-dict"
        with pytest.raises(MissingContextKeyError):
            build_authoritative_context(source)


class TestContextBuilderFromEntity:
    """Task 8.9: entity-document -> AuthoritativeContext boundary."""

    def _entity(self) -> EntityDocument:
        return EntityDocument(
            id="product:P001",
            entity_type="product",
            revision=3,
            name="Kem ABC",
            facts=[
                Fact(
                    key="commerce.price.current",
                    type="int",
                    value=299000,
                    unit="VND",
                    revision=2,
                    freshness="volatile",
                    updated_at="2026-08-01T00:00:00+00:00",
                ),
                Fact(
                    key="commerce.promotion",
                    type="str",
                    value="Giảm 20%",
                    revision=1,
                    freshness="volatile",
                    updated_at="2026-08-01T00:00:00+00:00",
                ),
                Fact(
                    key="identity.sku",
                    type="str",
                    value="KEM-ABC-01",
                    revision=1,
                    freshness="stable",
                ),
            ],
            relations=[
                Relation(target_entity_id="shop:S1", relation_type="belongs_to_shop"),
            ],
        )

    def _shop(self) -> EntityDocument:
        return EntityDocument(
            id="shop:S1",
            entity_type="shop",
            revision=1,
            name="LIVENTO",
        )

    def test_builds_context_from_entity_facts(self) -> None:
        ctx = build_authoritative_context_from_entity(
            entity=self._entity(),
            shop=self._shop(),
            persona_text="Minh, thân thiện",
            campaign=EntityDocument(
                id="campaign:C1",
                entity_type="campaign",
                revision=1,
                name="Back to school",
            ),
        )

        assert isinstance(ctx, AuthoritativeContext)
        assert ctx.shop["name"] == "LIVENTO"
        assert ctx.persona["text"] == "Minh, thân thiện"
        assert ctx.campaign["name"] == "Back to school"
        assert ctx.product["id"] == "product:P001"
        assert ctx.product["name"] == "Kem ABC"
        # Fact records carry a stable per-fact id: entity id + fact key.
        assert {f["id"] for f in ctx.facts} == {
            "product:P001:commerce.price.current",
            "product:P001:commerce.promotion",
            "product:P001:identity.sku",
        }
        assert any(promo["id"] == "product:P001:commerce.promotion" for promo in ctx.promotions)

    def test_campaign_and_persona_optional(self) -> None:
        ctx = build_authoritative_context_from_entity(
            entity=self._entity(),
            shop=self._shop(),
            persona_text="",
        )

        assert ctx.persona == {}
        assert ctx.campaign == {}
        assert ctx.facts

    def test_missing_shop_raises(self) -> None:
        with pytest.raises(MissingContextKeyError):
            build_authoritative_context_from_entity(
                entity=self._entity(),
                shop=None,
                persona_text="",
            )


# ---------------------------------------------------------------------------
# 6.2 transition-policy context


class TestTransitionContext:
    def test_order_agnostic_strips_adjacent_summaries(self) -> None:
        ctx = build_transition_context(
            "ORDER_AGNOSTIC",
            previous_product_summary="Sản phẩm trước: sữa tắm",
            next_product_summary="Sản phẩm sau: dầu gội",
        )
        assert ctx.policy == "ORDER_AGNOSTIC"
        assert ctx.previous_product_summary is None
        assert ctx.next_product_summary is None
        guidance = transition_guidance(ctx)
        assert "Do NOT reference any previous or next product" in guidance
        assert "Sản phẩm trước" not in guidance

    def test_order_aware_keeps_summaries(self) -> None:
        ctx = build_transition_context(
            "ORDER_AWARE",
            previous_product_summary="Sữa tắm ABC",
            next_product_summary="Dầu gội XYZ",
        )
        assert ctx.previous_product_summary == "Sữa tắm ABC"
        assert ctx.next_product_summary == "Dầu gội XYZ"

    def test_transition_context_is_typed_pydantic(self) -> None:
        ctx = TransitionContext(policy="ORDER_AWARE", previous_product_summary="  ")
        assert ctx.previous_product_summary is None  # blank normalized to None

    def test_script_intent_carries_duration(self) -> None:
        intent = ScriptIntent(intent="CTA", target_duration_s=60)
        assert intent.target_duration_s == 60


# ---------------------------------------------------------------------------
# 6.3 generate prompt builder


class TestGeneratePrompt:
    def test_include_skill_constraints_context_duration_assignment(self) -> None:
        parts = _build_generate(build_transition_context("ORDER_AGNOSTIC"))
        assert "SKILL_MARKER_PRESENT" in parts.system
        assert "CLAIM_PRICE" in parts.system
        assert "Authoritative context" in parts.context
        assert "Kem ABC" in parts.context
        assert "Requested spoken duration: 600 seconds" in parts.context
        assert "FEATURE_BENEFIT" in parts.context
        assert "segment 1" in parts.user
        assert "GENERATE_SCRIPT_SEGMENT" in parts.user
        assert "FACT-1" in parts.context  # continuity carried fact

    def test_plan_assignment_rendered(self) -> None:
        parts = _build_generate(build_transition_context("ORDER_AGNOSTIC"))
        assert "## Plan assignment" in parts.user
        assert "segments: 3" in parts.user

    def test_order_aware_renders_summaries(self) -> None:
        parts = _build_generate(
            build_transition_context(
                "ORDER_AWARE",
                previous_product_summary="Sữa tắm ABC",
                next_product_summary="Dầu gội XYZ",
            )
        )
        assert "Sữa tắm ABC" in parts.context
        assert "Dầu gội XYZ" in parts.context

    def test_order_agnostic_prompt_never_bakes_product_dependency(self) -> None:
        parts = _build_generate(build_transition_context("ORDER_AGNOSTIC"))
        assert "Do NOT reference any previous or next product" in parts.context

    def test_continuity_never_full_prior_script(self) -> None:
        long_prior = build_tail("x" * 2000)
        state = ContinuityState(
            previous_segment_tail=long_prior, covered_fact_ids=frozenset({"F1"})
        )
        parts = build_generate_prompt(
            skill_text="skill",
            generation_constraints=[],
            context=build_authoritative_context(_full_context_dict()),
            duration_s=300,
            intent=ScriptIntent(intent="CORE_CONTENT", target_duration_s=300),
            transition=build_transition_context("ORDER_AGNOSTIC"),
            continuity=state,
        )
        assert len(state.previous_segment_tail) <= 300
        assert len(parts.context) < 3000  # bounded context, no full script


# ---------------------------------------------------------------------------
# 6.4 repair prompt builder


class TestRepairPrompt:
    def test_exact_failed_rules_and_source_immutable(self) -> None:
        parts = _build_repair()
        assert "Kem ABC giá 100.000đ, giảm 50%." in parts.user  # source verbatim
        assert "CLAIM_PRICE" in parts.context
        assert "CLAIM_DISCOUNT" in parts.context
        assert "Use only the authoritative price." in parts.context

    def test_forbids_broad_rewrite_new_claim_new_cta(self) -> None:
        parts = _build_repair()
        text = parts.system
        assert "MINIMAL changes" in text
        assert "Do NOT rewrite the script" in text
        assert "do NOT add new claims" in text
        assert "do NOT add new calls to action" in text

    def test_only_anti_drift_facts_injected(self) -> None:
        parts = _build_repair()
        assert "299.000đ" in parts.context
        assert "Giảm 20%" in parts.context
        # Persona/campaign are not needed to prevent drift and are absent.
        assert "persona" not in parts.context
        assert "campaign" not in parts.context

    def test_mismatched_instructions_raise(self) -> None:
        with pytest.raises(PromptBuildError):
            build_repair_prompt(
                source_text="text",
                failed_rule_ids=["CLAIM_PRICE", "CLAIM_DISCOUNT"],
                rule_repair_instructions=["only one instruction"],
                authoritative_facts=build_authoritative_context(_full_context_dict()),
            )

    def test_empty_source_raises(self) -> None:
        with pytest.raises(PromptBuildError):
            build_repair_prompt(
                source_text="  ",
                failed_rule_ids=["CLAIM_PRICE"],
                rule_repair_instructions=["fix it"],
                authoritative_facts=build_authoritative_context(_full_context_dict()),
            )

    def test_no_failed_rules_raises(self) -> None:
        with pytest.raises(PromptBuildError):
            build_repair_prompt(
                source_text="text",
                failed_rule_ids=[],
                rule_repair_instructions=[],
                authoritative_facts=build_authoritative_context(_full_context_dict()),
            )


# ---------------------------------------------------------------------------
# 6.5 prompt-contract tests: Generate and Fix separation


class TestGenerateFixContract:
    def test_generate_has_generate_marker_repair_has_repair_marker(self) -> None:
        generate = _build_generate(build_transition_context("ORDER_AGNOSTIC"))
        repair = _build_repair()
        assert "GENERATE_SCRIPT_SEGMENT" in generate.parts_keywords
        assert "REPAIR_SCRIPT_SEGMENT" in repair.parts_keywords

    def test_generate_excludes_repair_only_instructions(self) -> None:
        parts = _build_generate(build_transition_context("ORDER_AGNOSTIC"))
        joined = parts.system + parts.context + parts.user
        assert "failed rule" not in joined.lower()
        assert "Do NOT rewrite the script" not in joined
        assert "MINIMAL changes" not in joined

    def test_repair_excludes_sales_skill(self) -> None:
        parts = _build_repair()
        joined = parts.system + parts.context + parts.user
        assert "SKILL_MARKER_PRESENT" not in joined
        # The skill's creative guidance terms never appear in Fix.
        assert "GENERATE_SCRIPT_SEGMENT" not in joined

    def test_repair_excludes_unrelated_rules(self) -> None:
        parts = _build_repair()
        assert "FORMAT_EM_DASH" not in parts.context
        assert "VN_SPELLING" not in parts.context

    def test_repair_keywords_hold_failed_rule_ids(self) -> None:
        parts = _build_repair()
        assert "CLAIM_PRICE" in parts.parts_keywords
        assert "CLAIM_DISCOUNT" in parts.parts_keywords


# ---------------------------------------------------------------------------
# 6.6 contract tests: no tools / no iteration API in prompt+context modules


def _module_sources() -> list[tuple[str, str]]:
    base = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "backend"
        / "application"
        / "script_authoring"
        / "generation"
    )
    names = [
        "context_builder.py",
        "prompt_builder.py",
        "intent.py",
        "continuity.py",
    ]
    return [(name, (base / name).read_text(encoding="utf-8")) for name in names]


# Import prefixes that would give a model filesystem/web/job access; the
# word "tool" in prose is fine, an actual `tools=`/`import httpx` is not.
_FORBIDDEN_IMPORT_PREFIXES: tuple[str, ...] = (
    "from pathlib",
    "import os",
    "from os",
    "import sys",
    "from sys",
    "import subprocess",
    "import httpx",
    "import requests",
    "import urllib",
    "import websocket",
    "import asyncio",
    "import concurrent",
    "import threading",
    "import redis",
    "import sqlalchemy",
    "import livekit",
    "from livekit",
)
# API-shaped identifiers that only occur when a tool/function-calling
# schema is actually exposed.
_TOOL_SCHEMA_PATTERNS: tuple[str, ...] = (
    r"\btools\s*=",
    r"\btool_choice\b",
    r"\bfunction_calling\b",
    r"\bfunctions\s*=",
    r"\btool_calls\b",
    r"\btool_use\b",
)
# Model-controlled iteration entry points: loops that could drive further
# calls or let the model extend work. Plain `for` over a local dict in a
# renderer is not model-controlled iteration and is allowed.
_ITERATION_PATTERNS: tuple[str, ...] = (
    r"\bwhile\s+",
    r"\bretry\b",
    r"\bnext_segment\b",
    r"\bgather\s*\(",
    r"\bcreate_task\s*\(",
)


class TestNoToolsContract:
    @pytest.mark.parametrize(
        "name", ["context_builder.py", "prompt_builder.py", "intent.py", "continuity.py"]
    )
    def test_no_forbidden_tool_patterns(self, name: str) -> None:
        _, source = next(item for item in _module_sources() if item[0] == name)
        hits: list[str] = []
        for label, pattern in (
            ("filesystem/web/job import", _FORBIDDEN_IMPORT_PREFIXES),
            ("tool-schema identifier", _TOOL_SCHEMA_PATTERNS),
            ("iteration entry point", _ITERATION_PATTERNS),
        ):
            for needle in pattern:
                if re.search(needle, source):
                    hits.append(f"{label}:{needle}")
        assert not hits, f"{name} exposes forbidden patterns: {hits}"

    def test_no_llm_engine_imports_in_context_builder(self) -> None:
        _, source = next(item for item in _module_sources() if item[0] == "context_builder.py")
        assert "LLMEngine" not in source
        assert "llm_engines" not in source

    def test_prompt_parts_carries_no_tools(self) -> None:
        parts = _build_generate(build_transition_context("ORDER_AGNOSTIC"))
        assert parts.model_fields_set or True  # constructible shell
        assert not hasattr(parts, "tools")
        assert not hasattr(parts, "iteration")

    def test_builder_functions_take_no_tool_args(self) -> None:
        sig = inspect.signature(build_generate_prompt)
        for param in sig.parameters.values():
            assert param.name not in {"tools", "functions", "tool_choice"}


# ---------------------------------------------------------------------------
# 6.7 token-budget guards


class TestTokenBudgetGuards:
    def test_estimate_tokens_chars_over_four(self) -> None:
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("") == 1  # never zero
        assert estimate_tokens("a" * 400) == 100

    def test_normal_budget_passes(self) -> None:
        parts = _build_generate(build_transition_context("ORDER_AGNOSTIC"))
        returned = guard_budget(parts, max_tokens=100_000)
        assert returned is parts

    def test_oversized_raises_typed_error_not_truncation(self) -> None:
        parts = build_repair_prompt(
            source_text="Kem ABC giá 100.000đ.",
            failed_rule_ids=["CLAIM_PRICE"],
            rule_repair_instructions=["Use the authoritative price."],
            authoritative_facts=build_authoritative_context(_full_context_dict()),
        )
        with pytest.raises(OversizedContextError) as excinfo:
            guard_budget(parts, max_tokens=2)
        assert excinfo.value.actual_tokens > 2
        # Critical constraints survive intact — nothing was silently cut.
        assert "Use the authoritative price." in parts.context

    def test_oversized_error_is_value_error_subtype(self) -> None:
        assert issubclass(OversizedContextError, ValueError)
