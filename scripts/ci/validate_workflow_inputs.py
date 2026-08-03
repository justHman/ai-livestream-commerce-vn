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

Canonical service identifiers follow the design / refactor-microservices-monorepo
vocabulary: `backend_service`, `llm_service`, `tts_service`, `avatar_service`.
Release image tags use the short service name (`backend-v1.2.0`), mapped below.
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

# ── Supported services ──────────────────────────────────────────────────────

# Canonical product service identifiers (design §4 dispatch contract).
PRODUCT_SERVICE_IDS = frozenset({"backend_service", "llm_service", "tts_service", "avatar_service"})

# All dispatch-validated service identifiers.
SUPPORTED_SERVICES: frozenset[str] = PRODUCT_SERVICE_IDS

# Canonical identifier -> short identifier used in release image tags.
SERVICE_SHORT = {
    "backend_service": "backend",
    "llm_service": "llm",
    "tts_service": "tts",
    "avatar_service": "avatar",
}

# Allowed service prefixes for release tags `<short>-vMAJOR.MINOR.PATCH`.
SERVICE_TAG_NAMES = frozenset(SERVICE_SHORT.values())

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
    - Mixed case / non-lowercase characters
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


DEFAULT_SERVICE_ALLOWLIST = SUPPORTED_SERVICES


def validate_services(
    services_str: str,
    allowed: Optional[Set[str]] = None,
) -> List[str]:
    """Validate a comma-separated canonical service list.

    Returns a deduplicated, sorted list of canonical service identifiers
    (e.g. ``backend_service``). Raises ValueError on empty, unknown, mixed-case,
    empty-entry, or shell-metacharacter input.
    """
    allowed = allowed or DEFAULT_SERVICE_ALLOWLIST

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


def validate_service_tag(tag: str) -> Dict[str, str]:
    """Validate a service release tag of the form `<short>-vSEMVER`.

    Returns ``{"service", "version", "tag"}`` on success, where ``service`` is
    the derived canonical service (e.g. ``backend`` stays ``backend``).
    Raises ValueError on malformed tag or unsupported service name.
    """
    tag = tag.strip()

    if not tag:
        raise ValueError("Tag must not be empty.")

    m = re.match(
        r"^([a-z][a-z0-9_]*)-v(\d+)\.(\d+)\.(\d+)$",
        tag,
    )
    if not m:
        raise ValueError(
            f"Tag '{tag}' does not match '<service>-vMAJOR.MINOR.PATCH' pattern. "
            f"Service must be one of: {', '.join(sorted(SERVICE_TAG_NAMES))}."
        )

    service_short = m.group(1)
    if service_short not in SERVICE_TAG_NAMES:
        raise ValueError(
            f"Tag '{tag}' uses unsupported service '{service_short}'. "
            f"Service must be one of: {', '.join(sorted(SERVICE_TAG_NAMES))}."
        )

    # Map short tag service -> canonical identifier.
    canonical = next(k for k, v in SERVICE_SHORT.items() if v == service_short)

    return {
        "service": canonical,
        "service_short": service_short,
        "version": f"v{m.group(2)}.{m.group(3)}.{m.group(4)}",
        "tag": tag,
    }


# ── GITHUB_OUTPUT ───────────────────────────────────────────────────────────


def emit_github_output(key: str, value: str) -> None:
    """Emit a key=value pair to GITHUB_OUTPUT."""
    output_path = Path(os.environ.get("GITHUB_OUTPUT", "/dev/stdout"))
    with open(output_path, "a") as f:
        f.write(f"{key}={value}\n")


def emit_github_json_output(key: str, value: object) -> None:
    """Emit a JSON value (joined via heredoc) to GITHUB_OUTPUT."""
    output_path = Path(os.environ.get("GITHUB_OUTPUT", "/dev/stdout"))
    json_str = json.dumps(value, separators=(",", ":"))
    with open(output_path, "a") as f:
        f.write(f"{key}<<EOF\n{json_str}\nEOF\n")


# ── CLI ─────────────────────────────────────────────────────────────────────

REQUIRED_INPUTS = frozenset({"sha", "env", "services"})


def main(argv: Optional[List[str]] = None) -> None:
    """CLI entry point for validating workflow dispatch inputs.

    Usage:
        python scripts/ci/validate_workflow_inputs.py \\
            --sha <commit-sha> \\
            --env <environment> \\
            --services <comma-separated-canonical-list> \\
            [--require sha,env,services] [--github-output]
    """
    import argparse

    parser = argparse.ArgumentParser(
        description="Validate workflow dispatch inputs (SHA, environment, services)."
    )
    parser.add_argument("--sha", help="Full 40-hex commit SHA to validate")
    parser.add_argument("--env", help="Target environment (dev/staging/prod)")
    parser.add_argument(
        "--services",
        help="Comma-separated canonical service identifiers",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="Repository root for git resolution (default: cwd)",
    )
    parser.add_argument(
        "--require",
        default="sha,env,services",
        help="Which inputs are required (comma list of sha/env/services). Defaults to all three.",
    )
    parser.add_argument(
        "--github-output",
        action="store_true",
        help="Emit validated values to GITHUB_OUTPUT",
    )

    args = parser.parse_args(argv)

    required: Set[str] = {r.strip() for r in args.require.split(",") if r.strip()}
    unknown_required = required - REQUIRED_INPUTS
    if unknown_required:
        print(
            f"ERROR: unknown --require values: {', '.join(sorted(unknown_required))}. "
            f"Allowed: sha, env, services.",
            file=sys.stderr,
        )
        sys.exit(2)

    errors: List[str] = []
    sha = None
    env = None
    services = None

    if args.sha:
        try:
            sha = validate_sha(args.sha, args.repo_root)
        except ValueError as e:
            errors.append(str(e))
    elif "sha" in required:
        errors.append("Missing required input: --sha")

    if args.env:
        try:
            env = validate_environment(args.env)
        except ValueError as e:
            errors.append(str(e))
    elif "env" in required:
        errors.append("Missing required input: --env")

    if args.services:
        try:
            services = validate_services(args.services)
        except ValueError as e:
            errors.append(str(e))
    elif "services" in required:
        errors.append("Missing required input: --services")

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(1)

    if args.github_output:
        assert sha is not None
        emit_github_output("validated_sha", sha)
        assert env is not None
        emit_github_output("validated_env", env)
        assert services is not None
        emit_github_output("validated_services", ",".join(services))
        emit_github_json_output("services_matrix", services)

    result: Dict[str, Any] = {"sha": sha, "env": env, "services": services}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main(sys.argv[1:])
