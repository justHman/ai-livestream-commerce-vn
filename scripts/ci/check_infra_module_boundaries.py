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

    # 4. Environment roots: distinct state keys, no workspaces, no cross-state refs.
    keys = []
    for env in INFRA.joinpath("environments").iterdir():
        backend = env / "backend.tf"
        if not backend.exists():
            continue
        text = backend.read_text(encoding="utf-8")
        m = re.search(r'key\s*=\s*"([^"]+)"', text)
        keys.append(m.group(1) if m else None)
        if '"workspace' in text:
            errors.append(f"{backend}: uses Terraform workspaces")
        for tf in env.glob("*.tf"):
            if re.search(r'terraform_remote_state|data\s+"terraform_remote_state"', tf.read_text(encoding="utf-8")):
                errors.append(f"{tf}: cross-state reference")
    if len(keys) != len(set(keys)):
        errors.append(f"duplicate state keys: {keys}")

    # 5. LLM/TTS independence: no combined llm_tts task/service/capacity refs.
    for f in (INFRA / "modules" / "compute").glob("*.tf"):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'resource\s+"(aws_ecs_task_definition|aws_ecs_service|aws_ecs_capacity_provider|aws_launch_template|aws_autoscaling_group)"\s+"([a-z0-9_]+)"', text):
            if "llm_tts" in m.group(2):
                errors.append(f"{f}: combined llm_tts block {m.group(2)!r}")
        if re.search(r'container_name\s*=\s*"tts"', text) and re.search(r'container_name\s*=\s*"llm"', text):
            errors.append(f"{f}: combined llm+tts containers in one service/task")
        # 6. No standalone LMCache: no dedicated launch template/ASG/CP/task/service.
        for m in re.finditer(r'resource\s+"(aws_ecs_task_definition|aws_ecs_service|aws_ecs_capacity_provider|aws_launch_template|aws_autoscaling_group)"\s+"lmcache"', text):
            errors.append(f"{f}: standalone LMCache block {m.group(2)!r}")
        # 7. No self-host LiveKit: no livekit ECS task/service/SG/capacity.
        for m in re.finditer(r'resource\s+"(aws_ecs_task_definition|aws_ecs_service|aws_ecs_capacity_provider|aws_launch_template|aws_autoscaling_group|aws_security_group)"\s+"livekit"', text):
            errors.append(f"{f}: self-host LiveKit block {m.group(2)!r}")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"OK: {len(CANONICAL)} canonical modules, no forbidden roots or references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
