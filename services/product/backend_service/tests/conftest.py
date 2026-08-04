from __future__ import annotations
from pathlib import Path
import sys

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Canonical sibling service packages (llm.*, tts.*, avatar.*) — same
# PYTHONPATH layout the backend Dockerfile uses. COPY-DON'T-IMPORT: these
# packages are the canonical self-contained copies, not core shims.
_PRODUCT = Path(__file__).resolve().parents[2]
for _sibling in ("llm_service", "tts_service", "avatar_service"):
    _path = str(_PRODUCT / _sibling / "src")
    if _path not in sys.path:
        sys.path.insert(0, _path)
