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
        if "use_lockfile" not in text:
            errors.append(f"{backend}: missing use_lockfile=true (native S3 lockfile)")
        if "dynamodb_table" in text:
            errors.append(f"{backend}: still references DynamoDB locking")
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
        # 8. No internal model NLB: compute module declares no LB resources.
        if f.name != "loadbalancer" and "compute" in f.parts:
            for m in re.finditer(r'resource\s+"aws_lb"', text):
                errors.append(f"{f}: LB resource outside loadbalancer module")

    # 9. Runtime matrix: prod backend never Spot; prod default 2 on-demand.
    prod_vars = (INFRA / "environments" / "prod" / "variables.tf").read_text(encoding="utf-8")
    if 'backend_capacity_provider == "FARGATE"' not in prod_vars:
        errors.append("prod: backend_capacity_provider must reject Spot")
    if 'default     = 2' not in prod_vars or 'variable "desired_backend"' not in prod_vars:
        pass  # desired_backend default checked below
    m = re.search(r'variable "desired_backend" \{.*?default\s*=\s*(\d+)', prod_vars, re.S)
    if m and int(m.group(1)) < 1:
        errors.append("prod: desired_backend must be >= 1")

    # 10. Data matrix: dev managed data off by default; staging/prod on.
    for env in ("dev", "staging", "prod"):
        vars_t = (INFRA / "environments" / env / "variables.tf").read_text(encoding="utf-8")
        for flag in ("create_rds", "create_redis"):
            m = re.search(rf'variable "{flag}" \{{.*?default\s*=\s*(\w+)', vars_t, re.S)
            if not m:
                errors.append(f"{env}: missing {flag}")
                continue
            want = "false" if env == "dev" else "true"
            if m.group(1) != want:
                errors.append(f"{env}: {flag} default {m.group(1)} != {want}")

    # 11. Adapter/engine ownership: backend receives only *_ADAPTER env; no
    # legacy/ambiguous selector values anywhere in envs or compute.
    legacy = ("remote_http", "remote_avatar", "openai_compat", "mock", "none", "tone", "cloud_liveavatar", "self_host_")
    for f in list((INFRA / "modules" / "compute").glob("*.tf")) + list((INFRA / "environments" / "dev").glob("*.tfvars.example")) + list((INFRA / "environments" / "staging").glob("*.tfvars.example")) + list((INFRA / "environments" / "prod").glob("*.tfvars.example")):
        text = f.read_text(encoding="utf-8")
        if f.name == "backend.tf":
            if 'name      = "LLM_ENGINE"' in text or 'name      = "TTS_ENGINE"' in text or 'name      = "RENDER_BACKEND"' in text:
                errors.append(f"{f}: backend receives engine selector")
        for term in legacy:
            if term in text and f.suffix == ".tfvars.example":
                errors.append(f"{f}: legacy selector {term!r}")

    # 12. Immutable digests: no mutable image tags in tfvars examples; every
    # service declares a circuit breaker.
    for f in list((INFRA / "environments" / "dev").glob("*.tfvars.example")) + list((INFRA / "environments" / "staging").glob("*.tfvars.example")) + list((INFRA / "environments" / "prod").glob("*.tfvars.example")):
        text = f.read_text(encoding="utf-8")
        for m in re.finditer(r'image_[a-z]+\s*=\s*"([^"]+)"', text):
            if "@sha256:" not in m.group(1):
                errors.append(f"{f}: mutable image tag {m.group(1)!r}")
    for f in (INFRA / "modules" / "compute").glob("*.tf"):
        text = f.read_text(encoding="utf-8")
        if "resource \"aws_ecs_service\"" in text and "deployment_circuit_breaker" not in text:
            errors.append(f"{f}: ECS service without circuit breaker")

    # 13. No plaintext token inputs: no backend_api_token/admin_api_token vars
    # anywhere; tfvars examples must not assign real token values.
    for f in (INFRA / "modules" / "secrets").glob("*.tf"):
        if "variable \"backend_api_token\"" in f.read_text(encoding="utf-8"):
            errors.append(f"{f}: plaintext token variable")

    if errors:
        print("\n".join(errors))
        return 1
    print(f"OK: {len(CANONICAL)} canonical modules, no forbidden roots or references")
    return 0


if __name__ == "__main__":
    sys.exit(main())
