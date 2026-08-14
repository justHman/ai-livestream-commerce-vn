"""Task 14.9: deterministic resume-bridge template + continuity predicate.

Proves: the template output is deterministic and parameterized only by the
script product; the bridge returns "" when disabled; ``should_speak_bridge``
decides continuity (same product -> no bridge, switched product -> bridge,
script finished -> no bridge, no previous product -> bridge).
"""

from __future__ import annotations

from backend.application.live_runtime.resume_bridge import (
    build_resume_bridge,
    should_speak_bridge,
)


def test_template_output_is_deterministic() -> None:
    bridge = build_resume_bridge("P010")

    assert bridge == "Rồi, em tiếp tục với P010 nhé."
    assert bridge == build_resume_bridge("P010")  # deterministic


def test_template_parameterized_by_product_only() -> None:
    bridge = build_resume_bridge("P020")

    assert bridge == "Rồi, em tiếp tục với P020 nhé."
    assert "P010" not in bridge


def test_custom_template_and_excerpt() -> None:
    bridge = build_resume_bridge(
        "P010",
        "Quay lại {product} nào cả nhà.",
        previous_product="P020",
        next_sentence_excerpt="Còn bây giờ em mời anh chị đặt ngay...",
    )

    assert bridge == "Quay lại P010 nào cả nhà."


def test_disabled_template_returns_empty() -> None:
    assert build_resume_bridge("P010", template=None) == ""
    assert build_resume_bridge("P010", template="") == ""


def test_bridge_never_needs_an_llm_call() -> None:
    # The bridge is a pure function of strings: no IO, no model, no clock.
    import inspect

    assert not inspect.iscoroutinefunction(build_resume_bridge)


def test_predicate_switched_product_speaks_bridge() -> None:
    assert (
        should_speak_bridge(
            config_enabled=True,
            script_finished=False,
            previous_product="P020",
            current_product="P010",
        )
        is True
    )


def test_predicate_same_product_no_bridge() -> None:
    assert (
        should_speak_bridge(
            config_enabled=True,
            script_finished=False,
            previous_product="P010",
            current_product="P010",
        )
        is False
    )


def test_predicate_script_finished_no_bridge() -> None:
    assert (
        should_speak_bridge(
            config_enabled=True,
            script_finished=True,
            previous_product="P020",
            current_product="P010",
        )
        is False
    )


def test_predicate_no_previous_product_bridge() -> None:
    assert (
        should_speak_bridge(
            config_enabled=True,
            script_finished=False,
            previous_product=None,
            current_product="P010",
        )
        is True
    )


def test_predicate_disabled_no_bridge() -> None:
    assert (
        should_speak_bridge(
            config_enabled=False,
            script_finished=False,
            previous_product="P020",
            current_product="P010",
        )
        is False
    )
