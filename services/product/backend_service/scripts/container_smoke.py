"""Production-shaped backend container smoke (audit R0.1 / Decision 12).

Proves, against a real built image (``--mode docker``, CI) or a faithful
filesystem simulation of the image layout (``--mode layout``, offline):

- Change-B skill + profanity + safety resources load from the artifact
  (packaging defect R0.1);
- the generation prompt seam is reached without LLM, network, or GPU;
- the app boots and reports liveness 200 + readiness 200 in the offline
  CI environment;
- a broken required dependency cannot be falsely green (readiness 503).

Exit codes: 0 all green; 1 regression; 2 the negative-readiness phase
found a not-ready app still returning HTTP 200 — that exact defect
(``R0.4``) is owned by Cluster-0 task 0.2 and becomes green once its
repair lands. Docker mode is the CI gate; layout mode is the offline
equivalent used when Docker is unavailable.

Stdlib only; list-args subprocess (no shell=True) for Windows safety.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Single source of truth for the in-artifact checks: `--mode layout` runs it
# against a simulated layout; `--mode docker` runs it INSIDE the image via
# `python -c`. The unit test imports this exact string so local proof ==
# in-image proof.
_ARTIFACT_CHECK_SRC = r"""
from backend.application.script_authoring.generation.skill_loader import SkillLoader

skill = SkillLoader()


def _check(label: str, fn) -> None:
    fn()
    print(f"PASS {label}")


def _skill_content() -> None:
    text = skill.content()
    if not text.strip():
        raise SystemExit("FAIL skill content empty")
    if "name: livestream-sales-script" not in text:
        raise SystemExit("FAIL skill content missing frontmatter name")


def _skill_hash() -> None:
    if len(skill.content_hash()) != 64:
        raise SystemExit("FAIL skill content_hash not sha256")


def _skill_version() -> None:
    version = skill.skill_version()
    if not version or not version[0].isdigit():
        raise SystemExit(f"FAIL skill_version invalid: {version!r}")


_check("skill.content()", _skill_content)
_check("skill.content_hash()", _skill_hash)
_check("skill.skill_version()", _skill_version)

from backend.application.script_authoring.gate.rules.profanity import load_curated_lexicon


def _profanity() -> None:
    lexicon = load_curated_lexicon()
    if not lexicon.version or not lexicon.words:
        raise SystemExit("FAIL profanity lexicon empty")


_check("profanity.curated_lexicon", _profanity)

from backend.application.safety_gate.resources import load_all_curated_patterns


def _safety() -> None:
    sets = load_all_curated_patterns()
    if set(sets) != {"toxicity", "harassment", "unsafe_content"}:
        raise SystemExit(f"FAIL safety kinds: {sorted(sets)}")
    if not all(s.patterns for s in sets.values()):
        raise SystemExit("FAIL safety patterns empty")


_check("safety.curated_patterns", _safety)

from backend.application.script_authoring.generation.prompt_builder import build_generate_prompt
from backend.application.script_authoring.generation.context_builder import AuthoritativeContext
from backend.application.script_authoring.generation.intent import ScriptIntent, TransitionContext


def _generation_seam() -> None:
    parts = build_generate_prompt(
        skill_text=skill.content(),
        generation_constraints=[],
        context=AuthoritativeContext(),
        duration_s=30,
        intent=ScriptIntent(intent="preflight", target_duration_s=30),
        transition=TransitionContext(),
    )
    if "GENERATE_SCRIPT_SEGMENT" not in parts.user:
        raise SystemExit("FAIL generation seam marker missing")
    if not parts.system.strip():
        raise SystemExit("FAIL generation system blocks empty")


_check("generation.prompt_seam", _generation_seam)

print("ARTIFACT-CHECKS-OK")
"""

_OFFLINE_ENV: dict[str, str] = {
    "RENDER_BACKEND": "mock",
    "LLM_ENGINE": "none",
    "TTS_ENGINE": "tone",
    "DIRECTOR_ENABLED": "0",
    "APP_ENV": "dev",
}


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _http_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _wait_for(predicate, label: str, deadline_s: float = 120.0) -> None:
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:
            pass
        time.sleep(2)
    raise SystemExit(f"FAIL timeout waiting for {label}")


# ── docker mode ──────────────────────────────────────────────────────────


def artifact_checks_docker(image: str) -> None:
    proc = _run(
        ["docker", "run", "--rm", "--entrypoint", "python", image, "-c", _ARTIFACT_CHECK_SRC]
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr)
        raise SystemExit(f"FAIL artifact checks inside image {image}")
    print(f"[artifact] PASS inside image {image}")


def boot_probes_docker(image: str, keep: bool) -> None:
    port = _free_port()
    env_args = [f"-e{k}={v}" for k, v in _OFFLINE_ENV.items()]
    run = _run(["docker", "run", "-d", "-p", f"{port}:8800", *env_args, image])
    if run.returncode != 0:
        raise SystemExit(f"FAIL docker run: {run.stderr}")
    cid = run.stdout.strip()
    try:
        base = f"http://127.0.0.1:{port}"

        def _live() -> bool:
            return _http_get(f"{base}/health/live")[0] == 200

        _wait_for(_live, "liveness")
        status, body = _http_get(f"{base}/health/ready")
        if status != 200:
            raise SystemExit(f"FAIL /health/ready expected 200 got {status}: {body}")
        print("[boot] PASS liveness 200 + readiness 200")
    finally:
        if not keep:
            _run(["docker", "rm", "-f", cid])


def negative_readiness_docker(image: str, keep: bool) -> None:
    """Broken required dependency must never be falsely green (R0.4).

    ``TTS_ENGINE=transformers`` selects an engine whose dependency (torch)
    is deliberately absent from the production image, so the backend records
    a load error while still serving the tone stub. Readiness must then be
    503; until Cluster-0 task 0.2 lands it is 200 and this phase fails with
    exit code 2 (the defect is surfaced, not hidden).
    """
    port = _free_port()
    env_args = [f"-e{k}={v}" for k, v in _OFFLINE_ENV.items()] + ["-eTTS_ENGINE=transformers"]
    run = _run(["docker", "run", "-d", "-p", f"{port}:8800", *env_args, image])
    if run.returncode != 0:
        raise SystemExit(f"FAIL docker run (negative): {run.stderr}")
    cid = run.stdout.strip()
    try:
        base = f"http://127.0.0.1:{port}"

        def _live() -> bool:
            return _http_get(f"{base}/health/live")[0] == 200

        _wait_for(_live, "liveness (negative)")
        status, body = _http_get(f"{base}/health/ready")
        if status != 503:
            raise SystemExit(
                2, f"FAIL readiness falsely green (R0.4, owned by task 0.2): HTTP {status} {body}"
            )
        print("[negative-readiness] PASS /health/ready 503 when dependency broken")
    finally:
        if not keep:
            _run(["docker", "rm", "-f", cid])


# ── layout mode (offline, no Docker) ─────────────────────────────────────


def artifact_checks_layout(layout_root: str) -> None:
    root = Path(layout_root).resolve()
    target = root / "services" / "product" / "backend_service"
    if not (target / "src" / "backend").is_dir():
        raise SystemExit(
            f"FAIL layout root has no services/product/backend_service/src/backend: {root}"
        )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(target / "src")
    proc = subprocess.run(
        [sys.executable, "-c", _ARTIFACT_CHECK_SRC],
        cwd=target,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr)
        raise SystemExit(f"FAIL artifact checks in layout {root}")
    print(f"[artifact] PASS layout {root}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("docker", "layout"), required=True)
    parser.add_argument("--image", default="ai-live-backend:smoke")
    parser.add_argument("--layout", default="")
    parser.add_argument("--keep-containers", action="store_true")
    args = parser.parse_args()

    if args.mode == "layout":
        if not args.layout:
            parser.error("--layout <root> is required in layout mode")
        artifact_checks_layout(args.layout)
        return

    artifact_checks_docker(args.image)
    boot_probes_docker(args.image, args.keep_containers)
    negative_readiness_docker(args.image, args.keep_containers)


if __name__ == "__main__":
    main()
