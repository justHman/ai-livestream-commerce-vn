"""Backport PR #23558 cache-read into vLLM 0.22 registry.py.

vLLM 0.22 `_LazyRegisteredModel.inspect_model_cls` calls `_run_in_subprocess`
unconditionally. PR #23558 (merged after v0.22) reads _ModelInfo from a JSON
cache first and only falls back to subprocess on cache miss. This script patches
the installed registry.py to add the cache-read short-circuit, so a
pre-generated modelinfos/*.json lets vLLM 0.22 skip the subprocess that hangs
on L4 + receives SIGINT -> crash.

Idempotent: if already patched (cache-read present), no-op.

Usage: python scripts/patch_vllm_registry.py
  (auto-detects registry.py under site-packages/vllm/model_executor/models/)
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

OLD = (
    "    def inspect_model_cls(self) -> _ModelInfo:\n"
    "        return _run_in_subprocess(\n"
    "            lambda: _ModelInfo.from_model_cls(self.load_model_cls()))"
)

NEW = '''    def inspect_model_cls(self) -> _ModelInfo:
        # Backport of PR #23558: read _ModelInfo from JSON cache first to skip
        # _run_in_subprocess (hangs on L4 + SIGINT -> crash). Falls back to
        # subprocess on cache miss / stale hash.
        import hashlib as _h, json as _j, os as _os
        from dataclasses import asdict as _asdict
        from pathlib import Path as _Path
        model_path = _Path(__file__).parent / f"{self.module_name.split(".")[-1]}.py"
        mi = None
        if model_path.exists():
            module_hash = _h.md5(model_path.read_bytes()).hexdigest()
            cache_root = _os.environ.get("VLLM_CACHE_ROOT",
                                         _os.path.expanduser("~/.cache/vllm"))
            cache_file = (_Path(cache_root) / "modelinfos"
                          / f"{self.module_name}-{self.class_name}".replace(".", "-")
                          + ".json")
            try:
                if cache_file.exists():
                    d = _j.loads(cache_file.read_text())
                    if d.get("hash") == module_hash:
                        mi = _ModelInfo(**d["modelinfo"])
            except Exception:
                pass
        if mi is not None:
            return mi
        return _run_in_subprocess(
            lambda: _ModelInfo.from_model_cls(self.load_model_cls()))'''


def find_registry() -> Path | None:
    cands = [
        Path("/usr/local/lib/python3.12/dist-packages/vllm/model_executor/models/registry.py"),
        Path("/usr/local/lib/python3.11/dist-packages/vllm/model_executor/models/registry.py"),
    ]
    for c in cands:
        if c.exists():
            return c
    return None


def main() -> int:
    reg = find_registry()
    if reg is None:
        print("registry.py not found", file=sys.stderr)
        return 1
    s = reg.read_text()
    if "Backport of PR #23558" in s:
        print(f"already patched: {reg}")
        return 0
    if OLD not in s:
        print(f"OLD block not found in {reg} — vLLM version mismatch", file=sys.stderr)
        return 1
    reg.write_text(s.replace(OLD, NEW))
    print(f"patched: {reg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
