"""Swap task-def container image(s) to new SHA tag(s) and register a new revision.

Usage:
  python scripts/swap_task_image.py <cluster> <service> <container> <new_image>
  python scripts/swap_task_image.py <cluster> <service> <container>=<new_image> [<container>=<new_image> ...]
  python scripts/swap_task_image.py <cluster> <service> --base-task <task-def-arn-or-name> <container>=<new_image> ...

Prints the new task-definition ARN. Handles multi-container task defs. By
default the base is the service's current task-def; use --base-task to start
from a specific revision (e.g. the Terraform-managed revision with the right
env, not a prior swap-task rev with stale env).
"""
from __future__ import annotations

import json
import subprocess
import sys


def aws(args: list[str]) -> str:
    r = subprocess.run(["aws", *args], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"aws {args[0]} failed: {r.stderr}")
    return r.stdout


def main() -> int:
    args = sys.argv[1:]
    if "--base-task" in args:
        idx = args.index("--base-task")
        base_task = args[idx + 1]
        args = args[:idx] + args[idx + 2:]
    else:
        base_task = None
    cluster, service = args[0], args[1]
    swaps: dict[str, str] = {}
    rest = args[2:]
    if len(rest) == 2 and "=" not in rest[0]:
        swaps[rest[0]] = rest[1]
    else:
        for pair in rest:
            if "=" not in pair:
                sys.exit(f"bad arg {pair!r}; use container=image")
            k, _, v = pair.partition("=")
            swaps[k.strip()] = v.strip()
    if not swaps:
        sys.exit("no container=image pairs given")

    if base_task is None:
        base_task = aws(["ecs", "describe-services", "--cluster", cluster, "--services", service,
                         "--query", "services[0].taskDefinition", "--output", "text"]).strip()
    print(f"base task: {base_task}")
    td = json.loads(aws(["ecs", "describe-task-definition", "--task-definition", base_task,
                         "--output", "json"]))
    task = td["taskDefinition"]
    for c in task["containerDefinitions"]:
        if c["name"] in swaps:
            print(f"  {c['name']}: {c['image']} -> {swaps[c['name']]}")
            c["image"] = swaps[c["name"]]
    for k in ("taskDefinitionArn", "revision", "status", "requiresAttributes",
              "compatibilities", "registeredAt", "registeredBy", "deregisteredAt"):
        task.pop(k, None)
    out = aws(["ecs", "register-task-definition", "--cli-input-json",
               json.dumps(task), "--query", "taskDefinition.taskDefinitionArn",
               "--output", "text"])
    new_task = out.strip()
    print(f"new task: {new_task}")
    aws(["ecs", "update-service", "--cluster", cluster, "--service", service,
         "--task-definition", new_task, "--query", "service.taskDefinition",
         "--output", "text"])
    print(f"service {service} updated to {new_task}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
