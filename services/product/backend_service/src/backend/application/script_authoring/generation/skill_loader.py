"""Project-owned livestream-sales-script skill loader (tasks 5.6/5.7).

Loads the packaged ``resources/skills/livestream-sales-script/SKILL.md``
from the service-level resources directory (mirrors the repository's
resource convention, see ``script_authoring/gate/rules/profanity.py``).
Pure file read + SHA-256 hashing: no LLM, no network, no third-party
skill content (Design Decision 18 — the runtime never fetches a mutable
remote skill).

Generate loads this skill; Fix/repair never does (task 5.7). The version
comes from the SKILL.md frontmatter; the stable content hash is the
SHA-256 of the exact file bytes and is recorded in
``GenerationFingerprint.skill_version`` for reproducibility (Decision 13).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

SKILL_FILENAME = "SKILL.md"
_SKILL_NAME = "livestream-sales-script"
_VERSION_RE = re.compile(r'^version:\s*(\S+)\s*$', re.MULTILINE)
_DEFAULT_VERSION = "1.0.0"
_MAX_SKILL_BYTES = 256 * 1024  # bounded reasonable size for a markdown skill

# Service-level ``resources/`` directory, resolved relative to this package
# (4 levels: generation -> script_authoring -> application -> backend ->
# src -> backend_service), so it works from a source checkout and inside
# the Docker image, where ``resources/`` ships alongside ``src/backend/``.
_DEFAULT_SKILL = (
    Path(__file__).resolve().parents[6]
    / "resources"
    / "skills"
    / _SKILL_NAME
    / SKILL_FILENAME
)
# If the package is relocated deeper (e.g. an installed wheel), fall back
# to locating the service-level resources directory by name.
for _candidate in Path(__file__).resolve().parents:
    _resources = _candidate / "resources" / "skills" / _SKILL_NAME / SKILL_FILENAME
    if _resources.is_file():
        _DEFAULT_SKILL = _resources
        break


class SkillNotFoundError(RuntimeError):
    """Raised when the packaged skill file cannot be loaded.

    The message names the exact path that was tried so a missing resource
    fails loudly instead of silently generating without guidance.
    """


def _read_skill(path: Path) -> str:
    """Read and validate the skill file at ``path`` (pure file I/O)."""
    if not path.is_file():
        raise SkillNotFoundError(
            f"packaged skill not found: {_SKILL_NAME} (tried {path})"
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise SkillNotFoundError(
            f"cannot read packaged skill {_SKILL_NAME}: {path} ({exc})"
        ) from exc
    if len(raw) > _MAX_SKILL_BYTES:
        raise SkillNotFoundError(
            f"packaged skill {_SKILL_NAME} exceeds size limit: {path}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SkillNotFoundError(
            f"packaged skill {_SKILL_NAME} is not valid UTF-8: {path}"
        ) from None
    if not text.strip():
        raise SkillNotFoundError(f"packaged skill {_SKILL_NAME} is empty: {path}")
    return text


def _extract_version(text: str) -> str:
    """Return the frontmatter ``version:`` value, falling back to the default."""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else _DEFAULT_VERSION


class SkillLoader:
    """Loads the packaged skill and exposes stable content + hash.

    The loader is stateless and re-reads the file on every call; the file
    is immutable at runtime, so hashes are stable. Generate callers use
    ``content()`` for the system prompt and record ``content_hash()`` /
    ``skill_version()`` in the GenerationFingerprint.
    """

    def __init__(self, skill_path: Path | None = None) -> None:
        """Use the packaged resource by default; ``skill_path`` is test-only."""
        self._path = skill_path or _DEFAULT_SKILL

    def content(self) -> str:
        """Return the full skill text (validated, bounded, non-empty)."""
        return _read_skill(self._path)

    def content_hash(self) -> str:
        """Return the stable SHA-256 of the exact skill file bytes."""
        return hashlib.sha256(_read_skill(self._path).encode("utf-8")).hexdigest()

    def skill_version(self) -> str:
        """Return the skill version from the file frontmatter (stable)."""
        return _extract_version(self.content())

    def path(self) -> str:
        """Return the resolved skill file path (for diagnostics only)."""
        return str(self._path)


__all__ = [
    "SkillLoader",
    "SkillNotFoundError",
    "SKILL_FILENAME",
]
