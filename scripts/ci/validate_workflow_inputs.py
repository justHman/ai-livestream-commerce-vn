"""
Task 81 — OpenSpec 2.2: Validated workflow inputs for immutable commit SHA,
target environment, supported service identifiers.

Smallest reusable validation shared by dispatch/release workflows:
- Full 40-hex SHA resolving in repo
- Environment exact allowlist per workflow
- Services comma-list normalized/deduped/nonempty subset of supported services
- No shell injection or whitespace ambiguity
- Emit safe JSON/matrix outputs via GITHUB_OUTPUT, never eval
- Validate branch ancestry / CI separately in owning workflows
"""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Set

# ── Supported services ──────────────────────────────────────────────────────

# Product services (project-owned, independently deployable)
PRODUCT_SERVICES = frozenset({"backend", "llm", "tts", "avatar"})

# Platform services (upstream runtimes with project config/assets)
PLATFORM_SERVICES = frozenset({"livekit", "lmcache"})

# All supported service identifiers
SUPPORTED_SERVICES: frozenset[str] = PRODUCT_SERVICES | PLATFORM_SERVICES

# Service → short identifier used in image tags, ECS service names
SERVICE_SHORT = {
    "backend": "backend",
    "llm": "llm",
    "tts": "tts",
    "avatar": "avatar",
    "livekit": "livekit",
    "lmcache": "lmcache",
}

# ── Supported environments ──────────────────────────────────────────────────

SUPPORTED_ENVIRONMENTS = frozenset({"dev", "staging", "prod"})


# ── Validation ──────────────────────────────────────────────────────────────


def validate_sha(sha: str, repo_root: Optional[Path] = None) -> str:
    """Validate a full 40-hex commit SHA that resolves in the repository.

    Returns the normalized SHA (lowercase).

    Raises ValueError on:
    - Leading/trailing whitespace (whitespace ambiguity)
    - Not a 40-hex string
    - Does not resolve to a commit in the repo
    """
    if sha != sha.strip():
        raise ValueError(
            "SHA must not have leading or trailing whitespace (avoid shell/input ambiguity)."
        )

    sha = sha.lower()

    if not re.match(r"^[0-9a-f]{40}$", sha):
        raise ValueError(
            f"Invalid SHA format: '{sha}'. Must be a full 40-character hexadecimal commit SHA."
        )

    repo_root = repo_root or Path.cwd()
    try:
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=30,
        )
    except FileNotFoundError:
        raise ValueError("Git is not available on this system.")
    except subprocess.TimeoutExpired:
        raise ValueError(f"Git operation timed out resolving SHA '{sha}'.")

    if result.returncode != 0:
        raise ValueError(f"SHA '{sha}' does not resolve to any object in the repository.")

    obj_type = result.stdout.strip()
    if obj_type != "commit":
        raise ValueError(f"SHA '{sha}' resolves to a '{obj_type}' object, not a commit.")

    return sha


def validate_environment(env: str, allowed: Optional[Set[str]] = None) -> str:
    """Validate a target environment string.

    Returns the normalized (lowercase) environment name.

    Raises ValueError on:
    - Empty or whitespace-only
    - Contains characters other than lowercase letters (a-z)
    - Not in the allowed set
    """
    if env != env.strip():
        raise ValueError("Environment must not have leading or trailing whitespace.")

    if not env:
        raise ValueError("Environment must not be empty.")

    if env != env.lower():
        raise ValueError(f"Invalid environment: '{env}'. Must be lowercase (a-z).")

    if not re.match(r"^[a-z]+$", env):
        raise ValueError(
            f"Invalid environment: '{env}'. Must contain only lowercase letters (a-z)."
        )

    allowed = allowed or SUPPORTED_ENVIRONMENTS
    if env not in allowed:
        raise ValueError(
            f"Unsupported environment: '{env}'. Must be one of: {', '.join(sorted(allowed))}."
        )

    return env


def validate_services(services_str: str, allowed: Optional[Set[str]] = None) -> List[str]:
    """Validate a comma-separated service list.

    Returns a deduplicated, sorted list of service identifiers.

    Raises ValueError on:
    - Empty string after stripping
    - Unknown service identifier
    - Whitespace-only entries
    """
    allowed = allowed or SUPPORTED_SERVICES

    if not services_str or not services_str.strip():
        raise ValueError("Service list must not be empty.")

    raw = [s.strip() for s in services_str.split(",")]
    seen: Set[str] = set()
    result: List[str] = []
    errors: List[str] = []

    for entry in raw:
        if not entry:
            errors.append("Empty entry in service list (consecutive commas).")
            continue
        if entry != entry.lower():
            errors.append(f"Service '{entry}' must be lowercase (got mixed case).")
            continue
        if entry in seen:
            continue
        if entry not in allowed:
            errors.append(
                f"Unknown service: '{entry}'. Must be one of: {', '.join(sorted(allowed))}."
            )
            continue
        seen.add(entry)
        result.append(entry)

    if errors:
        raise ValueError("Service validation errors:\n  " + "\n  ".join(errors))

    if not result:
        raise ValueError("No valid services remain after deduplication.")

    return sorted(result)


def validate_service_tag(tag: str) -> dict:
    """Validate a service release tag of the form <service>-vSEMVER.

    Returns dict with 'service', 'version', 'tag' on success.
    Raises ValueError on malformed tag.
    """
    tag = tag.strip()

    if not tag:
        raise ValueError("Tag must not be empty.")

    # Pattern: <service>-vMAJOR.MINOR.PATCH
    m = re.match(
        r"^(backend|llm|tts|avatar|livekit|lmcache)-v(\d+)\.(\d+)\.(\d+)$",
        tag,
    )
    if not m:
        raise ValueError(
            f"Tag '{tag}' does not match '<service>-vMAJOR.MINOR.PATCH' "
            f"pattern. Service must be one of: "
            f"{', '.join(sorted(SUPPORTED_SERVICES))}."
        )

    return {
        "service": m.group(1),
        "version": f"v{m.group(2)}.{m.group(3)}.{m.group(4)}",
        "tag": tag,
    }


# ── GITHUB_OUTPUT ───────────────────────────────────────────────────────────


def emit_github_output(key: str, value: str) -> None:
    """Emit a key=value pair to GITHUB_OUTPUT.

    Uses the GITHUB_OUTPUT environment variable if set, otherwise
    writes to stdout with the expected delimiter.
    """
    output_path = Path(__import__("os").environ.get("GITHUB_OUTPUT", "/dev/stdout"))
    # Simple value: no newlines, no special chars that need delimiting
    with open(output_path, "a") as f:
        f.write(f"{key}={value}\n")


def emit_github_json_output(key: str, value: object) -> None:
    """Emit a JSON value to GITHUB_OUTPUT."""
    output_path = Path(__import__("os").environ.get("GITHUB_OUTPUT", "/dev/stdout"))
    json_str = json.dumps(value, separators=(",", ":"))
    with open(output_path, "a") as f:
        f.write(f"{key}<<EOF\n{json_str}\nEOF\n")


# ── CLI entry point ─────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point for validating workflow dispatch inputs.

    Usage:
        python scripts/ci/validate_workflow_inputs.py \\
            --sha <commit-sha> \\
            --env <environment> \\
            --services <comma-separated-list>
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate workflow dispatch inputs (SHA, environment, services)."
    )
    parser.add_argument("--sha", help="Full 40-hex commit SHA to validate")
    parser.add_argument("--env", help="Target environment (dev/staging/prod)")
    parser.add_argument(
        "--services",
        help="Comma-separated list of service identifiers",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root for git resolution (default: cwd)",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Emit validated values to GITHUB_OUTPUT",
    )

    args = parser.parse_args()
    errors: List[str] = []

    sha = None
    env = None
    services = None

    if args.sha:
        try:
            sha = validate_sha(args.sha, args.repo_root)
        except ValueError as e:
            errors.append(str(e))

    if args.env:
        try:
            env = validate_environment(args.env)
        except ValueError as e:
            errors.append(str(e))

    if args.services:
        try:
            services = validate_services(args.services)
        except ValueError as e:
            errors.append(str(e))

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    if args.github_output:
        if sha:
            emit_github_output("validated_sha", sha)
        if env:
            emit_github_output("validated_env", env)
        if services is not None:
            emit_github_output("validated_services", ",".join(services))
            emit_github_json_output("services_matrix", services)

    result = {"sha": sha, "env": env, "services": services}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
