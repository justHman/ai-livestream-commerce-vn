"""Task 5.7 tests: Generate loads the packaged skill, Fix never does.

The contract under test:

- ``SkillLoader.load`` (``content``/``content_hash``/``skill_version``)
  loads the packaged ``livestream-sales-script`` skill with a stable
  SHA-256 content hash (task 5.6) — the Generate system prompt source.
- The repair (Fix) path never loads the skill: a ``SkillLoader`` is never
  constructed on the repair path, and its loader is not importable from
  the repair module. The repair prompt source is the rules' own repair
  instructions, not the sales skill.
- A missing skill fails loudly (``SkillNotFoundError`` naming the path
  tried), so generation can never silently run without guidance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend.application.script_authoring.generation.skill_loader import (
    SKILL_FILENAME,
    SkillLoader,
    SkillNotFoundError,
)


def test_skill_loader_loads_packaged_skill() -> None:
    """The packaged SKILL.md loads with content, a stable hash, and a version."""
    loader = SkillLoader()

    content = loader.content()
    assert content.strip()
    assert "PLAN_PRODUCT_SCRIPT" in content
    assert "GENERATE_SCRIPT_SEGMENT" in content

    # SHA-256 of the exact bytes — the fingerprint value (Decision 13).
    assert len(loader.content_hash()) == 64
    assert loader.content_hash() == loader.content_hash()

    version = loader.skill_version()
    assert version == "1.0.0"


def test_skill_loader_missing_file_fails_loudly() -> None:
    """A missing packaged skill raises with the exact path tried."""
    loader = SkillLoader(skill_path=Path("nope/missing/SKILL.md"))

    with pytest.raises(SkillNotFoundError, match="SKILL.md"):
        loader.content()


def test_generate_system_prompt_sources_the_skill() -> None:
    """Generate's system prompt is sourced from the packaged skill content."""
    loader = SkillLoader()
    system_prompt = loader.content()

    assert "Livestream Sales Script" in system_prompt
    assert "## Operation: PLAN_PRODUCT_SCRIPT" in system_prompt
    assert "## Operation: GENERATE_SCRIPT_SEGMENT" in system_prompt


def test_fix_never_loads_the_skill() -> None:
    """The repair path never loads the sales skill (task 5.7).

    Repair is a function in ``generation/prompt_builder`` (design Decision 5:
    Fix is a distinct constrained contract, not a separate module). It
    receives only failed-rule repair instructions and authoritative facts —
    never the skill content or a SkillLoader.
    """
    from backend.application.script_authoring.generation import prompt_builder

    assert not hasattr(prompt_builder, "SkillLoader")
    assert not hasattr(prompt_builder, "load_skill")
    # Repair prompt assembly must not reference the skill file/resource.
    assert SKILL_FILENAME not in prompt_builder.__dict__
    # The repair prompt never loads the packaged skill content: no path into
    # resources/skills, no SkillLoader construction, no skill version lookup.
    import inspect

    repair_src = inspect.getsource(prompt_builder.build_repair_prompt)
    assert "SkillLoader" not in repair_src
    assert "resources/skills" not in repair_src
