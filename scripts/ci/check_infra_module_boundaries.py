#!/usr/bin/env python3
"""Static assert: infra uses exactly the canonical module set, no service-named modules.

Run: python scripts/ci/check_infra_module_boundaries.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INFRA = ROOT / "infra"
CANONICAL = {"network", "security", "compute", "database", "loadbalancer", "storage", "secrets", "monitoring"}
FORBIDDEN = {"backend", "llm", "tts", "avatar", "renderer", "livekit", "lmcache", "workbench"}


def main() -> int:
    errors = []

    # 1. Module roots: exactly the canonical set, nothing else.
    roots = {p.name for p in INFRA.joinpath("modules").iterdir() if p.is_dir()}
    if roots != CANONICAL:
        errors.append(f"module roots mismatch: {sorted(roots)} != {sorted(CANONICAL)}")

    # 2. Module calls: only canonical names, only relative sources into modules/.
    call_re = re.compile(r'module\s+"([^"]+)"\s*\{')
    source_re = re.compile(r'^\s*source\s*=\s*"([^"]+)"', re.M)
    for env in INFRA.joinpath("environments").iterdir():
        for tf in env.glob("*.tf"):
            text = tf.read_text(encoding="utf-8")
            for match in call_re.finditer(text):
                name = match.group(1)
                body = text[match.end():]
                if name not in CANONICAL:
                    errors.append(f"{tf}: module {name!r} not in canonical set")
                m = source_re.search(body)
                if m and not m.group(1).startswith("../../modules/"):
                    errors.append(f"{tf}: module {name!r} source {m.group(1)!r} outside modules/")

    # 3. No module dir or call may carry a service name.
    for root in INFRA.joinpath("modules").iterdir():
        if root.name in FORBIDDEN:
            errors.append(f"service-named module dir: {root.name}")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"OK: {len(CANONICAL)} canonical modules, no forbidden roots or references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
