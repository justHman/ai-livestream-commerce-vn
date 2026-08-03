"""Generate the deterministic service contract (OpenAPI) excluding health.

Usage:
    python scripts/generate_contract.py <service_src_dir> <out_dir>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def generate(service_package: str, src_dir: Path, out_dir: Path) -> Path:
    """Build the app, dump OpenAPI excluding /health/*, write deterministically."""
    sys.path.insert(0, str(src_dir))
    if service_package == "llm":
        from llm import create_app
    elif service_package == "tts":
        from tts import create_app
    elif service_package == "avatar":
        from avatar import create_app
    else:
        raise ValueError(f"unknown service package {service_package}")

    app = create_app()
    spec = app.openapi()
    # Exclude health paths (unversioned, not part of the product contract).
    for path in [p for p in list(spec.get("paths", {})) if p.startswith("/health")]:
        del spec["paths"][path]

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "openapi.json"
    target.write_text(
        json.dumps(spec, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=["llm", "tts", "avatar"])
    parser.add_argument("--src", default="src")
    parser.add_argument("--out", default="contracts/v1")
    args = parser.parse_args()
    src = Path(args.src).resolve()
    out = Path(args.out).resolve()
    target = generate(args.service, src, out)
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())