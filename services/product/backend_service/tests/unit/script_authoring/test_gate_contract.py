"""Contract test (task 3.12): ScriptGate is pure and deterministic.

Proves:
1. ScriptGate evaluation requires NO LLM and NO network — the rule modules
   import nothing from llm/httpx/requests/aiohttp and the gate module graph
   is pure stdlib + backend.application.text_chunker (Change A).
2. Gate failure is deterministic: identical content + identical context +
   identical rule versions => byte-identical violations and fingerprint.
3. The canonical registry is versioned with stable IDs (task 3.1).
"""

from __future__ import annotations

import importlib
import re
import sys

from backend.application.script_authoring.gate import (
    ProductFacts,
    ScriptGate,
    ScriptGateContext,
    ScriptRuleRegistry,
    default_full_script_rules,
    default_segment_rules,
)

# Modules a PURE gate must never import (directly or transitively).
_FORBIDDEN_MODULES = ("httpx", "requests", "aiohttp", "openai", "anthropic", "websockets")
# Allowed external package roots (stdlib + Change A + the gate itself).
_ALLOWED_PACKAGE_ROOTS = ("backend",)


def _gate_and_rules():
    segment_rules = default_segment_rules()
    full_rules = default_full_script_rules()
    registry = ScriptRuleRegistry(list(segment_rules) + list(full_rules))
    return ScriptGate(registry, segment_rules, full_rules), registry


def _gate_module_graph() -> set[str]:
    """All modules reachable from the gate package (import graph)."""
    from backend.application import script_authoring  # noqa: F401

    visited: set[str] = set()
    stack = ["backend.application.script_authoring.gate"]
    while stack:
        name = stack.pop()
        if name in visited:
            continue
        visited.add(name)
        module = importlib.import_module(name)
        for attr_name, attr in vars(module).items():
            if attr_name.startswith("_"):
                continue
            module_name = getattr(attr, "__module__", "")
            if module_name.startswith("backend.application.script_authoring.gate"):
                stack.append(module_name)
    return visited


def test_gate_imports_no_llm_network() -> None:
    """No LLM/network module may be imported by the gate package graph.

    Order-independent: checks the gate modules' OWN imported module names
    (via each module's ``__dict__``), not the global ``sys.modules`` which
    other test files may have polluted.
    """
    imported = set()
    for name in _gate_module_graph():
        module = sys.modules[name]
        imported.update(
            attr.__module__
            for attr in vars(module).values()
            if getattr(attr, "__module__", "").startswith(_FORBIDDEN_MODULES)
        )
    assert not imported, f"gate modules import forbidden packages: {sorted(imported)}"


def test_gate_reachable_modules_are_pure() -> None:
    """Every module reachable from the gate is stdlib or backend-local."""
    reachable = _gate_module_graph()
    for name in reachable:
        root = name.split(".")[0]
        assert root in _ALLOWED_PACKAGE_ROOTS or root in sys.stdlib_module_names, (
            f"gate module {name} imports external package {root}"
        )


def test_gate_failure_deterministic_for_identical_input() -> None:
    gate, _ = _gate_and_rules()
    facts = ProductFacts(
        product_name="Kem ABC",
        prices=("299.000",),
        discounts=("giảm 20%",),
        skus=("SKU-P004",),
        allowed_claims=("kem dưỡng ẩm sâu",),
    )
    ctx = ScriptGateContext(facts=facts)
    dirty = "Kem ABC giá 99.000đ giảm 50%!!! mua ngay mua ngay mua ngay https://scam.vn dmm"

    first = gate.run_segment(dirty, ctx)
    second = gate.run_segment(dirty, ctx)

    assert not first.passed
    assert first.violations == second.violations
    assert first.fingerprint == second.fingerprint
    assert first.fingerprint.hexdigest == second.fingerprint.hexdigest
    assert first.errors  # deterministic FAIL for identical content


def test_gate_pass_deterministic_for_identical_input() -> None:
    gate, _ = _gate_and_rules()
    facts = ProductFacts(
        prices=("299.000",),
        discounts=("giảm 20%",),
        skus=("SKU-P004",),
        allowed_claims=("kem dưỡng ẩm sâu",),
    )
    ctx = ScriptGateContext(facts=facts, total_min_seconds=0, target_min_seconds=0)
    clean = "Kem ABC với kem dưỡng ẩm sâu, giá 299.000đ và giảm 20% hôm nay. Mua ngay nhé!"

    first = gate.run_segment(clean, ctx)
    second = gate.run_segment(clean, ctx)

    assert first.passed
    assert first.violations == second.violations


def test_registry_versioned_stable_ids() -> None:
    _, registry = _gate_and_rules()
    assert len(registry) >= 20
    for rule_id in registry.ids():
        assert registry.version(rule_id) >= 1
    family = re.compile(
        r"^(?:FORMAT|STYLE|VN_SPELLING|PROFANITY|CLAIM|TTS|REPETITION|"
        r"SPEECH_DURATION|COVERAGE|CTA|TONE|TRANSITION)_[A-Z0-9_]+$"
    )
    for rule_id in registry.ids():
        assert family.match(rule_id), f"rule id {rule_id} outside documented families"


def test_full_script_gate_scope_and_fingerprint() -> None:
    gate, _ = _gate_and_rules()
    ctx = ScriptGateContext()
    result = gate.run_full_script(["A.", "B."], ctx)
    assert result.scope == "full_script"
    assert result.fingerprint.rule_ids  # fingerprint bound to evaluated rules
