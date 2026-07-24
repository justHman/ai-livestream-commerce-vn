"""Swap backend task-def image to a new SHA tag and register a new revision.

Usage: python scripts/swap_task_image.py <cluster> <service> <container> <new_image>
Prints the new task-definition ARN.
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
    cluster, service, container, new_image = sys.argv[1:5]
    old_task = aws(["ecs", "describe-services", "--cluster", cluster, "--services", service,
                    "--query", "services[0].taskDefinition", "--output", "text"]).strip()
    print(f"old task: {old_task}")
    td = json.loads(aws(["ecs", "describe-task-definition", "--task-definition", old_task,
                         "--output", "json"]))
    task = td["taskDefinition"]
    found = False
    for c in task["containerDefinitions"]:
        if c["name"] == container:
            c["image"] = new_image
            found = True
    if not found:
        sys.exit(f"container {container} not found in task def")
    for k in ("taskDefinitionArn", "revision", "status", "requiresAttributes",
              "compatibilities", "registeredAt", "registeredBy", "deregisteredAt"):
        task.pop(k, None)
    print(f"new image: {new_image}")
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
