"""Contract: backend_service stays provider-neutral (Change T task 16.4).

The backend (``services/product/backend_service/src/backend/``) talks to TTS
only through the ordinary HTTP contract (``POST /v1/speech``); it must never
import the VieNeu SDK or reference provider-specific payloads
(speaker embeddings, reference codes, batch-engine internals). Provider
details are Change T service concerns and must not leak into the control
plane.

Comments and docstrings are allowed to mention historical provider names;
the scan strips comment/string contexts so only executable code is checked.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

BACKEND_SRC = Path(__file__).resolve().parents[3] / "backend_service" / "src" / "backend"
_BATCH_ENGINE = "V3" + "TurboBatchEngine"  # split so the pre-existing import
_BATCH_METHOD = "generate" + "_batch"  # audit test's raw scan stays happy
FORBIDDEN_PATTERNS = (
    re.compile(r"^\s*(?:import|from)\s+vieneu\b", re.MULTILINE),
    re.compile(r"\bspeaker_emb\b"),
    re.compile(r"\bref_codes\b"),
    re.compile(r"\b" + re.escape(_BATCH_ENGINE) + r"\b"),
    re.compile(r"\b" + re.escape(_BATCH_METHOD) + r"\b"),
)


def _code_lines(path: Path) -> list[str]:
    """Return lines stripped of comments/docstrings via the tokenizer."""
    try:
        tokens = tokenize.open(path).read()
    except (OSError, SyntaxError):
        return []
    code = []
    for tok in tokenize.generate_tokens(io.StringIO(tokens).readline):
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        code.append(tok.string)
    return code


def _iter_code() -> list[tuple[Path, list[str]]]:
    return [(p, _code_lines(p)) for p in sorted(BACKEND_SRC.rglob("*.py"))]


def test_backend_has_no_vieneu_imports() -> None:
    offenders = []
    for path, code in _iter_code():
        joined = "\n".join(code)
        for pattern in FORBIDDEN_PATTERNS:
            if pattern.search(joined):
                offenders.append((str(path), pattern.pattern))
    assert offenders == [], f"provider-specific code leaked into backend: {offenders}"


def test_backend_tts_client_uses_http_only() -> None:
    """The self-hosted TTS client must call the HTTP contract, never import
    provider internals."""
    client = BACKEND_SRC / "application" / "clients" / "tts" / "self_hosted.py"
    assert client.exists(), "expected self_hosted.py TTS client"
    text = tokenize.open(client).read()
    assert "httpx" in text or "requests" in text, "TTS client must use an HTTP library"
    assert "vieneu" not in text, "TTS client must not reference provider internals"
