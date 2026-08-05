"""Fixed-file Vietnamese prompt bundle loader.

Ownership per OpenSpec 1.12:

- Accepts only fixed symbolic bundle names (``PromptName``) for the four known
  Markdown files. A caller can never supply a filesystem path, ``..``, an
  absolute path, a URL, a symlink escape, or an environment override.
- Validates all four files at first load: exists, is a regular file inside the
  owned prompt directory, decodes as UTF-8, is non-empty after trim, and stays
  within a bounded size.
- Caches immutable static text and a stable content hash/revision after
  validation; repeated loads never re-read disk.
- Exposes bundle identity/hash/token-count estimate without exposing prompt
  text in logs or exception messages.
- Failures name the missing/broken bundle (never unrelated path contents or
  customer data).

The loader owns no templating and performs no composition; see ``composer.py``
for decision/fallback flows.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from importlib import resources
from pathlib import Path
from types import MappingProxyType

_MAX_PROMPT_BYTES = 64 * 1024  # bounded reasonable size per file
_TOKEN_RE = re.compile(r"[\wÀ-ỹ]+|[^\w\s]", re.UNICODE)


class PromptName(str, Enum):
    """Fixed symbolic bundle names — the only accepted loader key."""

    BASE_SALES = "base_sales_vi"
    DIRECTOR_DECISION = "director_decision_vi"
    RESPONSE_GUARDRAILS = "response_guardrails_vi"
    FALLBACK_RESPONSE = "fallback_response_vi"


ALL_PROMPT_NAMES: frozenset[str] = frozenset(name.value for name in PromptName)


@dataclass(frozen=True, slots=True)
class PromptPair:
    """Static validated prompt text plus its stable content hash."""

    filename: str
    text: str
    sha256: str
    bytes: int
    tokens: int


@dataclass(frozen=True, slots=True)
class PromptBundle:
    """Validated, cached bundle of the four fixed prompts with aggregate metadata."""

    version: int = 1
    files: frozenset[str] = frozenset(ALL_PROMPT_NAMES)
    prompts: Mapping[str, PromptPair] = None  # type: ignore[assignment]
    content_hash: str = ""
    total_bytes: int = 0
    total_tokens: int = 0
    token_counts: Mapping[str, int] = None  # type: ignore[assignment]
    byte_counts: Mapping[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        """Wrap mutable dicts in immutable proxies so stored values cannot
        be mutated after construction."""
        # Use object.__setattr__ because the dataclass is frozen.
        object.__setattr__(self, "prompts", MappingProxyType(dict(self.prompts)))
        object.__setattr__(self, "token_counts", MappingProxyType(dict(self.token_counts)))
        object.__setattr__(self, "byte_counts", MappingProxyType(dict(self.byte_counts)))

    def prompt(self, name: str) -> str:
        try:
            return self.prompts[name].text
        except KeyError:
            raise KeyError(f"unknown prompt name {name!r}") from None

    def hash(self) -> str:
        return self.content_hash

    def metadata(self) -> dict[str, object]:
        """Safe metadata with NO prompt text — identity/hash/counts only."""
        return {
            "prompt_names": sorted(self.files),
            "content_hash": self.content_hash,
            "total_bytes": self.total_bytes,
            "total_tokens": self.total_tokens,
            "token_counts": dict(self.token_counts),
            "byte_counts": dict(self.byte_counts),
        }


class PromptBundleValidationError(Exception):
    """Raised when a fixed prompt file fails startup validation.

    The message names only the bundle file (or the class of failure) — never
    prompt contents, unrelated path contents, or customer data.
    """


def _estimate_tokens(text: str) -> int:
    """Rough whitespace-aware token estimate (documented as an estimate)."""
    return len(_TOKEN_RE.findall(text))


def _default_resource_dir() -> Path:
    """Return the on-disk directory that owns the four prompt files.

    Works both in a source checkout and inside an installed wheel: importlib
    resources is preferred, falling back to the adjacent package directory.
    """
    try:
        pkg = __import__("backend.application.director.prompts", fromlist=[""])
        with resources.as_file(resources.files(pkg)) as root:
            return Path(root)
    except Exception:
        here = Path(__file__).resolve().parent
        if here.name == "prompts":
            return here
        raise


def _resolve_name(name: str) -> str:
    """Reject anything that is not one of the fixed symbolic names."""
    try:
        return PromptName(name).value
    except ValueError:
        raise PromptBundleValidationError(
            f"prompt name {name!r} is not a fixed bundle name"
        ) from None


def _validate_dir_safe(path: Path) -> None:
    """Reject if the path or any of its ancestors is a symlink.

    A canonical loader must not follow symlinks into arbitrary directories,
    even when the final target is a regular file or directory.
    """
    abs_path = path.absolute()
    walk = abs_path
    while walk != walk.parent:
        try:
            if walk.is_symlink():
                raise PromptBundleValidationError(f"symlink ancestry is not allowed: {walk}")
        except OSError as exc:
            raise PromptBundleValidationError(f"cannot inspect path ancestry: {walk}") from exc
        walk = walk.parent


def _validate_file(path: Path, name: str) -> PromptPair:
    if not path.exists():
        raise PromptBundleValidationError(f"missing prompt file: {name}")
    if path.is_symlink():
        raise PromptBundleValidationError(f"prompt file is a symlink: {name}")
    if not path.is_file():
        raise PromptBundleValidationError(f"prompt file is not a regular file: {name}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise PromptBundleValidationError(f"cannot read prompt file: {name}") from exc
    if len(raw) > _MAX_PROMPT_BYTES:
        raise PromptBundleValidationError(f"prompt file exceeds size limit: {name}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PromptBundleValidationError(f"prompt file is not valid UTF-8: {name}") from None
    if not text.strip():
        raise PromptBundleValidationError(f"prompt file is empty: {name}")
    sha = hashlib.sha256(raw).hexdigest()
    return PromptPair(
        filename=name,
        text=text,
        sha256=sha,
        bytes=len(raw),
        tokens=_estimate_tokens(text),
    )


def _load_from_directory(directory: Path) -> PromptBundle:
    """Load and validate all four fixed files from a known owned directory."""
    _validate_dir_safe(directory)
    if not directory.is_dir():
        raise PromptBundleValidationError("prompt directory is not a directory")
    prompts: dict[str, PromptPair] = {}
    for name in sorted(ALL_PROMPT_NAMES):
        prompts[name] = _validate_file(directory / f"{name}.md", name)
    digest = hashlib.sha256()
    for name in sorted(ALL_PROMPT_NAMES):
        digest.update(prompts[name].sha256.encode("utf-8"))
        digest.update(b"\x00")
    token_counts = {name: prompts[name].tokens for name in ALL_PROMPT_NAMES}
    byte_counts = {name: prompts[name].bytes for name in ALL_PROMPT_NAMES}
    return PromptBundle(
        files=frozenset(ALL_PROMPT_NAMES),
        prompts=prompts,
        content_hash=digest.hexdigest(),
        total_tokens=sum(token_counts.values()),
        total_bytes=sum(byte_counts.values()),
        token_counts=token_counts,
        byte_counts=byte_counts,
    )


def _load_bundle() -> PromptBundle:
    return _load_from_directory(_default_resource_dir())


@lru_cache(maxsize=1)
def load_bundle() -> PromptBundle:
    """Return the validated, cached prompt bundle (startup-validated, immutable).

    The cache is intentionally module-global and process-lifetime: after first
    load, source-file mutations do not change the active prompt bundle. Tests
    clear the cache explicitly to simulate a fresh process.
    """
    return _load_bundle()


def _load_bundle_from_dir(directory: Path) -> PromptBundle:
    """Load a bundle from a caller-supplied directory (test-only helper).

    Tests use this to validate loader behavior with synthetic directories
    (missing files, invalid content, symlinks, etc.). Production callers must
    use ``load_bundle()`` against the canonical owned resource directory.
    """
    return _load_from_directory(directory)


def bundle_metadata() -> dict[str, object]:
    """Safe bundle metadata (identity/hash/counts only — never prompt text)."""
    return load_bundle().metadata()


def bundle_content_hash() -> str:
    return load_bundle().content_hash


def bundle_token_count() -> int:
    return load_bundle().total_tokens


__all__ = [
    "ALL_PROMPT_NAMES",
    "PromptBundle",
    "PromptBundleValidationError",
    "PromptName",
    "PromptPair",
    "bundle_content_hash",
    "bundle_metadata",
    "bundle_token_count",
    "load_bundle",
]
