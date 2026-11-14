#!/usr/bin/env python3
"""Script-quality QA harness for Runtime scripts (task B0).

Two subcommands:

- ``analyze`` — fully offline: re-computes the metrics from
  ``script_quality_metrics`` on a saved JSON document (a single script doc or a
  ``run``-mode result file), prints a summary table and writes a report JSON.
- ``run`` — MANUAL ONLY: drives the real Runtime API (create script-set →
  generate → poll workflow → read parts/gate/fallback → regenerate gate-failed
  parts) for each product × ``--rounds`` rounds, then stores one JSON file the
  ``analyze`` command can consume. Every regeneration (flagged-part attempts
  and optional ``--regen-trials``) records the workflow status, error fields
  and the re-read gate state, so the run reports a real per-attempt success
  rate instead of the old "no part left flagged" flag. Safety rails:

  - the API token is read from the ``LIVENTO_QA_TOKEN`` environment variable
    and is **never** printed or written to any output file (only ever sent in
    the ``Authorization`` header);
  - the run refuses unless the workspace (script-set name prefix) starts with
    ``ZZ-TEST`` — QA data must never touch customer workspaces;
  - ``--dry-run`` prints the planned API calls without any network traffic;
  - only the Python standard library is used (``urllib``), no new dependency.

API surface used (see ``services/product/backend_service/src/backend/api/v1/
scripts.py`` — viewer-plane bearer auth):

- ``POST /api/v1/script-sets``                        create the QA script-set
- ``POST /api/v1/script-sets/{id}/generate-batch``  ordered_units, 202 + batch (Studio path)
- ``GET  /api/v1/script-sets/{id}/generation-batches/{bid}``       poll status
- ``GET  /api/v1/script-sets/{id}``                   parts + gate + fallback
- ``POST /api/v1/script-sets/{id}/products/{pid}/segments/{idx}/regenerate``

Exit codes: ``0`` success, ``1`` analyze found failing verdicts, ``2`` usage/
safety guard refused, ``3`` run finished but some API steps errored.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Make the module runnable as a plain script (`python scripts/qa/script_quality_qa.py`)
# where the repo root is not on sys.path yet.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.qa.script_quality_metrics import (  # noqa: E402
    analyze_script,
)

RESULTS_SCHEMA = "script_quality_qa_run/v1"
REPORT_SCHEMA = "script_quality_qa_report/v1"
PRODUCTS_SCHEMA = "script_quality_products/v1"
WORKSPACE_PREFIX = "ZZ-TEST"
_TOKEN_ENV = "LIVENTO_QA_TOKEN"
_REDACTED_TOKEN = "<LIVENTO_QA_TOKEN, redacted>"
_TERMINAL_JOB_STATUSES = frozenset({"completed", "failed", "cancelled", "timeout"})
_TERMINAL_BATCH_STATUSES = frozenset(
    {"completed", "partial_completed", "failed", "cancelled", "timeout"}
)


# ── Safety guards ────────────────────────────────────────────────────────────


def ensure_zz_test_workspace(workspace: str) -> None:
    """Refuse runs whose workspace (script-set name prefix) is not QA data."""
    if not workspace.startswith(WORKSPACE_PREFIX):
        raise SystemExit(
            f"refusing to run: workspace name must start with '{WORKSPACE_PREFIX}' "
            f"(got '{workspace}'). QA runs must use ZZ-TEST-* workspaces, never customer data."
        )


def _load_token() -> str:
    token = os.environ.get(_TOKEN_ENV, "").strip()
    if not token:
        raise SystemExit(
            f"refusing to run: environment variable {_TOKEN_ENV} is not set "
            "(the bearer token is only read from the environment, never from CLI args)"
        )
    return token


# ── HTTP (stdlib only; the token is never logged) ────────────────────────────


def _parse_json(body: str) -> dict:
    try:
        parsed = json.loads(body)
        return parsed if isinstance(parsed, dict) else {"raw": parsed}
    except ValueError:
        return {"raw": body}


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict | None,
    token: str,
    timeout_s: float = 60.0,
) -> tuple[int, dict]:
    """One JSON API call. Returns ``(status, parsed_body)``; 4xx/5xx are
    returned, not raised. The token travels in the Authorization header only —
    it never appears in any message, log line or stored result."""
    url = base_url.rstrip("/") + path
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, _parse_json(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:  # server answered with an error envelope
        return exc.code, _parse_json(exc.read().decode("utf-8", "replace"))


# ── Products fixture ─────────────────────────────────────────────────────────


def load_products(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    products = data.get("products") if isinstance(data, dict) else data
    if not isinstance(products, list) or not products:
        raise SystemExit(f"products file {path} must contain a non-empty 'products' list")
    for product in products:
        for key in ("id", "name", "allowed_claims"):
            if not product.get(key):
                raise SystemExit(f"product entry in {path} is missing required key '{key}'")
    return products


def product_facts_payload(product: dict) -> dict:
    """Wire shape of one product's authoritative facts (scripts.py brief)."""
    return {
        "product_name": product.get("name", ""),
        "prices": [str(p) for p in product.get("prices", [])],
        "discounts": [str(p) for p in product.get("discounts", [])],
        "skus": [str(s) for s in product.get("skus", [])],
        "allowed_claims": [str(c) for c in product.get("allowed_claims", [])],
    }


# ── Run: the call plan (used verbatim by --dry-run) ──────────────────────────


def build_plan(
    base_url: str,
    products: list[dict],
    rounds: int,
    workspace: str,
    target_duration_s: int,
    poll_interval_s: float,
    poll_timeout_s: float,
    regen_attempts: int,
    regen_trials: int = 0,
) -> list[dict]:
    """The exact call sequence ``run`` executes per product × round. Dry-run
    prints this; the real run follows the same shape (set/workflow ids are
    discovered from responses at runtime)."""
    plan: list[dict] = []
    for product in products:
        for round_no in range(1, rounds + 1):
            slug = product["id"]
            plan.append(
                {
                    "step": f"{slug} r{round_no}: create script-set",
                    "method": "POST",
                    "url": f"{base_url.rstrip('/')}/api/v1/script-sets",
                    "payload": {
                        "name": f"{workspace}-{slug}-r{round_no}",
                        "transition_policy": "ORDER_AGNOSTIC",
                        "product_ids": [product["id"]],
                        "brief": {"title": workspace, "product_facts": {product["id"]: "…"}},
                    },
                }
            )
            plan.append(
                {
                    "step": f"{slug} r{round_no}: generate",
                    "method": "POST",
                    "url": (f"{base_url.rstrip('/')}/api/v1/script-sets/{{set_id}}/generate-batch"),
                    "payload": {
                        "product_ids": [product["id"]],
                        "target_duration_s": target_duration_s,
                        "ordered_units": True,
                        "idempotency_key": "<uuid4>",
                    },
                }
            )
            plan.append(
                {
                    "step": f"{slug} r{round_no}: poll batch",
                    "method": "GET",
                    "url": (
                        f"{base_url.rstrip('/')}/api/v1/script-sets/{{set_id}}"
                        f"/generation-batches/{{batch_id}}"
                    ),
                    "payload": None,
                    "note": f"every {poll_interval_s:g}s, up to {poll_timeout_s:g}s",
                }
            )
            plan.append(
                {
                    "step": f"{slug} r{round_no}: read parts + gate/fallback",
                    "method": "GET",
                    "url": f"{base_url.rstrip('/')}/api/v1/script-sets/{{set_id}}",
                    "payload": None,
                }
            )
            if regen_attempts > 0:
                plan.append(
                    {
                        "step": f"{slug} r{round_no}: regenerate gate-failed/fallback parts",
                        "method": "POST",
                        "url": (
                            f"{base_url.rstrip('/')}/api/v1/script-sets/{{set_id}}"
                            f"/products/{product['id']}/segments/{{segment_index}}/regenerate"
                        ),
                        "payload": {"idempotency_key": "<uuid4>", "part_id": "<part_id?>"},
                        "note": f"up to {regen_attempts} attempts per part, only if needed",
                    }
                )
            if regen_trials > 0 and round_no == 1:
                plan.append(
                    {
                        "step": f"{slug} r{round_no}: regeneration trials (all segments)",
                        "method": "POST",
                        "url": (
                            f"{base_url.rstrip('/')}/api/v1/script-sets/{{set_id}}"
                            f"/products/{product['id']}/segments/{{segment_index}}/regenerate"
                        ),
                        "payload": {"idempotency_key": "<uuid4>", "part_id": "<part_id?>"},
                        "note": (
                            f"{regen_trials} sequential trials rotating segment_index, "
                            "independent of the gate flags"
                        ),
                    }
                )
    return plan


def print_plan(plan: list[dict]) -> None:
    print(f"Authorization: Bearer {_REDACTED_TOKEN}")
    for entry in plan:
        payload = (
            json.dumps(entry.get("payload"), ensure_ascii=False) if entry.get("payload") else ""
        )
        note = f"  ({entry['note']})" if entry.get("note") else ""
        print(f"[{entry['step']}] {entry['method']} {entry['url']} {payload}{note}".rstrip())
    print(f"plan: {len(plan)} calls, no network traffic in --dry-run")


# ── Run: real execution ──────────────────────────────────────────────────────


def _poll_workflow(
    base_url: str,
    token: str,
    set_id: str,
    product_id: str,
    workflow_id: str,
    interval_s: float,
    timeout_s: float,
) -> tuple[str, dict]:
    """Poll one generation/regeneration workflow until a terminal state."""
    path = f"/api/v1/script-sets/{set_id}/products/{product_id}/workflows/{workflow_id}"
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while True:
        _, body = request_json(base_url, "GET", path, None, token)
        status = str(body.get("status", ""))
        if status in _TERMINAL_JOB_STATUSES:
            return status, body
        if time.monotonic() > deadline:
            return "timeout", body
        time.sleep(interval_s)


def _poll_batch(
    base_url: str,
    token: str,
    set_id: str,
    batch_id: str,
    interval_s: float,
    timeout_s: float,
) -> tuple[str, dict]:
    """Poll one generation batch (the path the Studio uses) until a terminal state."""
    path = f"/api/v1/script-sets/{set_id}/generation-batches/{batch_id}"
    deadline = time.monotonic() + timeout_s
    body: dict = {}
    while True:
        _, body = request_json(base_url, "GET", path, None, token)
        status = str(body.get("status", ""))
        if status in _TERMINAL_BATCH_STATUSES:
            return status, body
        if time.monotonic() > deadline:
            return "timeout", body
        time.sleep(interval_s)


def _fetch_set(base_url: str, token: str, set_id: str) -> dict:
    status, body = request_json(base_url, "GET", f"/api/v1/script-sets/{set_id}", None, token)
    if status != 200:
        raise RuntimeError(f"GET script-set {set_id} returned HTTP {status}")
    return body


def _extract_error(body: dict) -> tuple[str | None, str | None]:
    """Pull ``error_code``/``error_message`` out of an API error body — both the
    ``{"error": {"code", "message"}}`` envelope and flat shapes."""
    if not isinstance(body, dict):
        return None, None
    error = body.get("error")
    if isinstance(error, dict):
        return (
            error.get("code") or body.get("error_code"),
            error.get("message") or body.get("error_message"),
        )
    return body.get("error_code"), body.get("error_message")


def _gate_summary(gate: object) -> tuple[str | None, int | None]:
    """``(state, error_violation_count)`` of a script-set gate, or ``(None,
    None)`` when the set carries no gate."""
    if not isinstance(gate, dict):
        return None, None
    violations = gate.get("violations") or []
    errors = sum(
        1
        for violation in violations
        if isinstance(violation, dict) and str(violation.get("severity", "")).lower() == "error"
    )
    return gate.get("state"), errors


def _regen_once(
    base_url: str,
    token: str,
    set_id: str,
    product_id: str,
    segment_index: object,
    part_id: object,
    interval_s: float,
    timeout_s: float,
) -> dict:
    """One regeneration execution: dispatch → poll workflow → re-read the set.

    The returned record carries the workflow status, ``error_code``/
    ``error_message`` when the body has them, and the post-regeneration gate
    state + error-violation count. The token never enters the record."""
    record: dict = {"segment_index": segment_index, "part_id": part_id}
    payload: dict = {"idempotency_key": uuid.uuid4().hex}
    if part_id:
        payload["part_id"] = part_id  # RegenerateReq prefers stable part identity
    status, body = request_json(
        base_url,
        "POST",
        f"/api/v1/script-sets/{set_id}/products/{product_id}/segments/{segment_index}/regenerate",
        payload,
        token,
    )
    record["accepted"] = status == 202
    if status != 202:
        error_code, error_message = _extract_error(body)
        record["status"] = status  # HTTP status: the workflow never started
        record["body"] = body
        if error_code:
            record["error_code"] = error_code
        if error_message:
            record["error_message"] = error_message
        return record
    workflow_id = str(body.get("workflow_id") or body.get("id") or "")
    record["workflow_id"] = workflow_id
    workflow_status, job_body = _poll_workflow(
        base_url, token, set_id, product_id, workflow_id, interval_s, timeout_s
    )
    record["status"] = workflow_status
    error_code, error_message = _extract_error(job_body)
    if error_code:
        record["error_code"] = error_code
    if error_message:
        record["error_message"] = error_message
    set_wire = _fetch_set(base_url, token, set_id)  # re-read: gate after this regen
    item = (set_wire.get("items") or {}).get(product_id) or {}
    gate_state, gate_errors = _gate_summary(item.get("gate"))
    record["gate_state_after"] = gate_state
    record["gate_error_count_after"] = gate_errors
    return record


def _regen_counts(round_results: list[dict]) -> dict:
    """Aggregate regeneration outcomes over a product's rounds: flagged-part
    attempts (``round.regen.attempts``) plus trials (``round.regen_trials``).

    - ``regen_attempt_success_rate`` — completed / total executions;
    - ``regen_clean_success_rate`` — completed AND the re-read gate has no
      error violations afterwards / total executions."""
    attempts = [
        attempt
        for result in round_results
        for attempt in (result.get("regen") or {}).get("attempts") or []
        if isinstance(attempt, dict)
    ]
    trials = [
        trial
        for result in round_results
        for trial in (result.get("regen_trials") or {}).get("trials") or []
        if isinstance(trial, dict)
    ]
    entries = attempts + trials
    total = len(entries)
    completed = sum(1 for entry in entries if entry.get("status") == "completed")
    clean = sum(
        1
        for entry in entries
        if entry.get("status") == "completed" and entry.get("gate_error_count_after") == 0
    )
    return {
        "regen_attempts_total": len(attempts),
        "regen_attempts_completed": sum(
            1 for attempt in attempts if attempt.get("status") == "completed"
        ),
        "regen_trials_total": len(trials),
        "regen_trials_completed": sum(1 for trial in trials if trial.get("status") == "completed"),
        "regen_clean_total": clean,
        "regen_attempt_success_rate": round(completed / total, 4) if total else None,
        "regen_clean_success_rate": round(clean / total, 4) if total else None,
    }


def _script_doc(set_wire: dict, product: dict) -> dict:
    """Extract the offline-analyzable script doc from a script-set wire."""
    item = (set_wire.get("items") or {}).get(product["id"]) or {}
    version = item.get("current_version") or {}
    sections = [
        {
            "text": part.get("display_text", ""),
            "role": ((part.get("brief") or {}).get("role") or ""),
        }
        for part in (version.get("parts") or [])
    ]
    return {
        "product": {key: product.get(key) for key in ("id", "name", "order_code")},
        "claims": list(product.get("allowed_claims", [])),
        "sections": sections,
    }


def _run_one_round(
    base_url: str,
    token: str,
    product: dict,
    round_no: int,
    workspace: str,
    target_duration_s: int,
    interval_s: float,
    timeout_s: float,
    regen_attempts: int,
    regen_trials: int = 0,
) -> dict:
    """Create → generate → poll → read gate/fallback → regenerate if needed."""
    slug = product["id"]
    errors: list[dict] = []

    status, set_wire = request_json(
        base_url,
        "POST",
        "/api/v1/script-sets",
        {
            "name": f"{workspace}-{slug}-r{round_no}",
            "transition_policy": "ORDER_AGNOSTIC",
            "product_ids": [slug],
            "brief": {"title": workspace, "product_facts": {slug: product_facts_payload(product)}},
        },
        token,
    )
    if status != 201:
        return {
            "round": round_no,
            "set_id": None,
            "api_errors": [{"step": "create", "status": status, "body": set_wire}],
        }
    set_id = str(set_wire.get("id", ""))

    # Same call the Studio makes: generate-batch with ordered_units (the per-product /generate
    # path is the legacy fixed-K route and does not exercise the production unit planner).
    status, generate = request_json(
        base_url,
        "POST",
        f"/api/v1/script-sets/{set_id}/generate-batch",
        {
            "product_ids": [slug],
            "target_duration_s": target_duration_s,
            "ordered_units": True,
            "idempotency_key": uuid.uuid4().hex,
        },
        token,
    )
    if status != 202:
        return {
            "round": round_no,
            "set_id": set_id,
            "api_errors": [{"step": "generate", "status": status, "body": generate}],
        }
    workflow_id = str(generate.get("batch_id") or "")
    job_status, job_body = _poll_batch(base_url, token, set_id, workflow_id, interval_s, timeout_s)
    round_result: dict = {
        "round": round_no,
        "set_id": set_id,
        "workflow": {
            "workflow_id": workflow_id,
            "status": job_status,
            **{k: job_body.get(k) for k in ("error_code", "error_message") if job_body.get(k)},
        },
        "api_errors": errors,
    }
    if job_status != "completed":
        round_result["gate"] = None
        round_result["script"] = _script_doc(_fetch_set(base_url, token, set_id), product)
        return round_result

    set_wire = _fetch_set(base_url, token, set_id)
    item = (set_wire.get("items") or {}).get(slug) or {}
    version = item.get("current_version") or {}
    parts = list(version.get("parts") or [])

    # Regenerate gate-failed/fallback parts. Each attempt records the workflow
    # status, error fields and the re-read gate state (see _regen_once).
    regen_log: dict = {"attempts": [], "success": True}
    for _attempt in range(max(0, regen_attempts)):
        needing = [p for p in parts if p.get("needs_regeneration")]
        if not needing:
            break
        target = needing[0]
        record = _regen_once(
            base_url,
            token,
            set_id,
            slug,
            target.get("segment_index"),
            target.get("part_id"),
            interval_s,
            timeout_s,
        )
        regen_log["attempts"].append(record)
        if not record["accepted"]:
            break  # the API refused the dispatch; retrying the same way is pointless
        set_wire = _fetch_set(base_url, token, set_id)
        item = (set_wire.get("items") or {}).get(slug) or {}
        version = item.get("current_version") or {}
        parts = list(version.get("parts") or [])
    still_needing = [p.get("segment_index") for p in parts if p.get("needs_regeneration")]
    regen_log["still_needing"] = still_needing
    regen_log["success"] = not still_needing

    # Optional --regen-trials: after the FIRST completed generation of each
    # product, regenerate every segment round-robin (by segment_index, not only
    # gate-flagged parts) N times sequentially. This is the measurement the
    # TASK_B §6.7 criterion needs (≥ 18 regens per product, ≥ 90% success).
    trials: list[dict] = []
    if regen_trials > 0 and parts:
        ordered = sorted(
            (p for p in parts if p.get("segment_index") is not None),
            key=lambda p: p["segment_index"],
        )
        for trial_no in range(regen_trials):
            if not ordered:
                break
            part = ordered[trial_no % len(ordered)]
            trials.append(
                _regen_once(
                    base_url,
                    token,
                    set_id,
                    slug,
                    part.get("segment_index"),
                    part.get("part_id"),
                    interval_s,
                    timeout_s,
                )
            )
            set_wire = _fetch_set(base_url, token, set_id)
            item = (set_wire.get("items") or {}).get(slug) or {}
            version = item.get("current_version") or {}
            parts = list(version.get("parts") or [])
            ordered = sorted(
                (p for p in parts if p.get("segment_index") is not None),
                key=lambda p: p["segment_index"],
            )

    round_result.update(
        {
            "state": str(item.get("state", "")),
            "gate": item.get("gate"),
            "fallback_parts": [
                {
                    "segment_index": p.get("segment_index"),
                    "source": p.get("source"),
                    "fallback_reason_code": p.get("fallback_reason_code"),
                    "fallback_reason_text": p.get("fallback_reason_text"),
                }
                for p in parts
                if p.get("source") == "fallback"
            ],
            "regen": regen_log,
            "script": _script_doc(set_wire, product),
        }
    )
    if regen_trials > 0:
        round_result["regen_trials"] = {"trials": trials}
    round_result["metrics"] = analyze_script(
        round_result["script"]["sections"],
        round_result["script"]["claims"],
        round_result["script"]["product"],
    )
    return round_result


def execute_run(
    base_url: str,
    token: str,
    products: list[dict],
    rounds: int,
    workspace: str,
    target_duration_s: int,
    interval_s: float,
    timeout_s: float,
    regen_attempts: int,
    regen_trials: int = 0,
) -> dict:
    product_results = []
    had_errors = False
    for product in products:
        round_results = [
            _run_one_round(
                base_url,
                token,
                product,
                round_no,
                workspace,
                target_duration_s,
                interval_s,
                timeout_s,
                regen_attempts,
                regen_trials,
            )
            for round_no in range(1, rounds + 1)
        ]
        had_errors = had_errors or any(r.get("api_errors") for r in round_results)
        metrics = [r.get("metrics") for r in round_results if r.get("metrics")]
        durations = [m["totals"]["estimated_duration_s"] for m in metrics]
        summary = {
            "rounds": len(round_results),
            "avg_estimated_duration_s": (
                round(sum(durations) / len(durations), 1) if durations else None
            ),
            "min_duration_pct_of_floor": (
                min((m["totals"]["duration_pct_of_floor"] for m in metrics), default=None)
            ),
            "gate_passed_all_rounds": all(
                (r.get("gate") or {}).get("state") == "passed"
                for r in round_results
                if r.get("gate") is not None
            ),
            "fallback_parts_total": sum(len(r.get("fallback_parts", [])) for r in round_results),
            # Renamed from regen_success_all_rounds: this only says no part is
            # still flagged needs_regeneration — it does NOT measure whether
            # each regeneration attempt itself succeeded. See the *_rate fields.
            "regen_no_part_left_flagged": all(
                r.get("regen", {}).get("success", True) for r in round_results
            ),
            "number_violations_total": sum(m["numbers"]["violation_count"] for m in metrics),
            "buzzword_hits_total": sum(m["buzzwords"]["hit_count"] for m in metrics),
        }
        summary.update(_regen_counts(round_results))
        product_results.append(
            {
                "product_id": product["id"],
                "product_name": product.get("name", ""),
                "rounds": round_results,
                "summary": summary,
            }
        )
    return {
        "schema": RESULTS_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "base_url": base_url.rstrip("/"),
        "workspace": workspace,
        "rounds_per_product": rounds,
        "products": product_results,
    }


# ── Analyze: input normalization + table ─────────────────────────────────────


def load_documents(path: Path) -> list[tuple[str, dict]]:
    """Yield ``(label, script_doc)`` pairs from a saved JSON file. Accepts a
    run-mode result file, a single script doc, or ``{"documents": [...]}``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and data.get("schema") == RESULTS_SCHEMA:
        docs = []
        for product in data.get("products", []):
            for round_result in product.get("rounds", []):
                doc = round_result.get("script")
                if not doc:
                    continue
                doc = dict(doc)
                doc["_label"] = (
                    f"{product.get('product_id', '?')} r{round_result.get('round', '?')}"
                )
                doc["_gate"] = round_result.get("gate")
                doc["_fallback_parts"] = round_result.get("fallback_parts")
                doc["_regen"] = round_result.get("regen")
                doc["_regen_trials"] = round_result.get("regen_trials")
                docs.append((doc["_label"], doc))
        if docs:
            return docs
    if isinstance(data, dict) and "documents" in data:
        return [(str(i), doc) for i, doc in enumerate(data["documents"])]
    if isinstance(data, dict) and "sections" in data:
        return [("single", data)]
    raise SystemExit(f"input file {path} is neither a run result nor a script document")


def _regen_entries(doc: dict) -> list[dict]:
    """Regeneration execution records attached to one analyzed document:
    flagged-part attempts (``_regen.attempts``) + trials (``_regen_trials``)."""
    attempts = (doc.get("_regen") or {}).get("attempts") or []
    trials = (doc.get("_regen_trials") or {}).get("trials") or []
    return [entry for entry in list(attempts) + list(trials) if isinstance(entry, dict)]


def _regen_rate(doc: dict, key: str) -> str:
    """Formatted per-round regen rate for the summary table ("—" when the
    round recorded no regeneration executions). ``key`` picks the metric:
    ``"completed"`` → attempt success rate, ``"clean"`` → clean-gate rate."""
    entries = _regen_entries(doc)
    if not entries:
        return "—"
    if key == "clean":
        good = sum(
            1
            for e in entries
            if e.get("status") == "completed" and e.get("gate_error_count_after") == 0
        )
    else:
        good = sum(1 for e in entries if e.get("status") == "completed")
    return f"{good / len(entries):.0%}"


def format_summary_table(rows: list[dict]) -> str:
    """One line per analyzed script with the headline numbers."""
    header = (
        f"{'label':<38} {'chars':>6} {'parts':>5} {'est_s':>7} {'%floor':>7} "
        f"{'gate':>12} {'regen':>6} {'clean':>6} {'num':>4} {'buzz':>4} {'reps':>4} "
        f"{'gap_s':>7} {'inter':>6}  verdict"
    )
    lines = [header, "-" * len(header)]
    for row in rows:
        gate = str(row.get("gate") or "—")[:12]
        lines.append(
            f"{row['label'][:38]:<38} {row['chars']:>6} {row['parts']:>5} "
            f"{row['est_s']:>7.1f} {row['pct_floor']:>6.1f}% {gate:>12} "
            f"{row['regen_rate']:>6} {row['clean_rate']:>6} "
            f"{row['number_violations']:>4} {row['buzzword_hits']:>4} {row['repeats']:>4} "
            f"{row['max_gap_s']:>7.1f} {row['interaction_ratio']:>6.1%}  {row['verdict']}"
        )
    return "\n".join(lines)


def analyze_document(doc: dict, floor_s: float | None, ceiling_s: float | None) -> dict:
    product = doc.get("product") or {}
    return analyze_script(
        doc.get("sections", []),
        doc.get("claims", []),
        product,
        floor_s=floor_s,
        ceiling_s=ceiling_s,
    )


def print_issue_details(label: str, analysis: dict, doc: dict) -> None:
    if analysis["passed"]:
        return
    print(f"\n[{label}] issues:")
    numbers = analysis["numbers"]
    for violation in numbers["digit_violations"][:5]:
        print(
            f"  - number '{violation['as_written']}' (unit '{violation['unit']}') "
            f"not in claims: …{violation['sentence'][-90:]}"
        )
    for violation in numbers["written_violations"][:5]:
        print(
            f"  - written number '{violation['as_written']}' = {violation['value']} "
            f"{violation['unit']} not in claims: …{violation['sentence'][-90:]}"
        )
    for violation in numbers.get("written_uncertain", [])[:5]:
        print(
            f"  - written number (uncertain) '{violation['as_written']}' = {violation['value']} "
            f"{violation['unit']} not clearly in claims: …{violation['sentence'][-90:]}"
        )
    for hit in analysis["buzzwords"]["hits"][:5]:
        print(f"  - buzzword '{hit['phrase']}' in section {hit['section']} (not in claims)")
    for repeat in analysis["repetition"]["verbatim_main_recap"][:5]:
        print(f"  - verbatim repeat main↔recap: '{repeat['sentence'][:90]}'")
    for opening in analysis["repetition"]["similar_openings"][:5]:
        print(f"  - similar openings '{opening['prefix']}': sections {opening['sections']}")
    flatness = analysis["flatness"]
    if flatness["interaction_count"] == 0:
        print("  - no interaction sentence (mời bình luận / hỏi đáp / nhắc chốt)")
    elif flatness["max_interaction_gap_s"] > flatness["interaction_target_s"]:
        print(
            f"  - interaction gap {flatness['max_interaction_gap_s']}s > "
            f"target {flatness['interaction_target_s']}s"
        )
    if "duration_below_70pct_floor" in analysis["issues"]:
        totals = analysis["totals"]
        print(
            f"  - estimated duration {totals['estimated_duration_s']}s is only "
            f"{totals['duration_pct_of_floor']}% of the {totals['target_floor_s']:g}s floor"
        )
    fallback = doc.get("_fallback_parts") or []
    if fallback:
        print(f"  - {len(fallback)} fallback part(s): {[f['segment_index'] for f in fallback]}")


def run_analyze(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    documents = load_documents(input_path)
    rows = []
    analyses = []
    for label, doc in documents:
        analysis = analyze_document(doc, args.floor_s, args.ceiling_s)
        analyses.append((label, doc, analysis))
        totals = analysis["totals"]
        gate = doc.get("_gate") or {}
        rows.append(
            {
                "label": label,
                "chars": totals["chars"],
                "parts": totals["sections"],
                "est_s": totals["estimated_duration_s"],
                "pct_floor": totals["duration_pct_of_floor"],
                "gate": gate.get("state"),
                "regen_rate": _regen_rate(doc, "completed"),
                "clean_rate": _regen_rate(doc, "clean"),
                "number_violations": analysis["numbers"]["violation_count"],
                "buzzword_hits": analysis["buzzwords"]["hit_count"],
                "repeats": (
                    len(analysis["repetition"]["verbatim_main_recap"])
                    + len(analysis["repetition"]["repeated_across_sections"])
                ),
                "max_gap_s": analysis["flatness"]["max_interaction_gap_s"],
                "interaction_ratio": analysis["flatness"]["interaction_ratio"],
                "verdict": "PASS" if analysis["passed"] else "FAIL",
            }
        )
    print(format_summary_table(rows))
    for label, doc, analysis in analyses:
        print_issue_details(label, analysis, doc)

    regen_entries = [entry for _label, doc, _analysis in analyses for entry in _regen_entries(doc)]
    regen_total = len(regen_entries)
    regen_completed = sum(1 for e in regen_entries if e.get("status") == "completed")
    regen_clean = sum(
        1
        for e in regen_entries
        if e.get("status") == "completed" and e.get("gate_error_count_after") == 0
    )
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "input": str(input_path),
        "documents": [
            {"label": label, "gate": doc.get("_gate"), "analysis": analysis}
            for label, doc, analysis in analyses
        ],
        "summary": {
            "documents": len(analyses),
            "passed": sum(1 for _, _, a in analyses if a["passed"]),
            "failed": sum(1 for _, _, a in analyses if not a["passed"]),
            "regen_attempt_success_rate": (
                round(regen_completed / regen_total, 4) if regen_total else None
            ),
            "regen_clean_success_rate": (
                round(regen_clean / regen_total, 4) if regen_total else None
            ),
        },
    }
    report_path = (
        Path(args.report) if args.report else input_path.with_name(input_path.stem + "_report.json")
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\nreport: {report_path}")
    print(f"summary: {report['summary']['passed']}/{report['summary']['documents']} passed")
    return 0 if report["summary"]["failed"] == 0 else 1


def run_real(args: argparse.Namespace) -> int:
    workspace = args.workspace or (
        f"{WORKSPACE_PREFIX}-script-qa-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}"
    )
    ensure_zz_test_workspace(workspace)  # hard guard: refuse customer workspaces
    products = load_products(Path(args.products))
    if args.only:
        products = [p for p in products if args.only in str(p.get("id", ""))]
        if not products:
            raise SystemExit(f"--only '{args.only}' matched no product in {args.products}")

    plan = build_plan(
        args.base_url,
        products,
        args.rounds,
        workspace,
        args.target_duration_s,
        args.poll_interval_s,
        args.poll_timeout_s,
        args.regen_attempts,
        args.regen_trials,
    )
    if args.dry_run:
        print_plan(plan)
        return 0

    token = _load_token()
    results = execute_run(
        args.base_url,
        token,
        products,
        args.rounds,
        workspace,
        args.target_duration_s,
        args.poll_interval_s,
        args.poll_timeout_s,
        args.regen_attempts,
        args.regen_trials,
    )
    out_path = (
        Path(args.out) if args.out else Path(f"{WORKSPACE_PREFIX.lower()}_script_qa_results.json")
    )
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote: {out_path}")
    for product in results["products"]:
        print(f"{product['product_id']}: {json.dumps(product['summary'], ensure_ascii=False)}")
    return 3 if any(p.get("api_errors") for pr in results["products"] for p in pr["rounds"]) else 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="script_quality_qa",
        description="Repeatable script-quality QA (task B0): offline analyze + manual real runs.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    analyze = sub.add_parser("analyze", help="offline analysis of a saved script/run JSON")
    analyze.add_argument("--input", required=True, help="path to the saved JSON document")
    analyze.add_argument(
        "--report",
        default=None,
        help="report output path (default: <input stem>_report.json next to the input)",
    )
    analyze.add_argument("--floor-s", type=float, default=None, help="override the duration floor")
    analyze.add_argument(
        "--ceiling-s", type=float, default=None, help="override the duration ceiling"
    )

    run = sub.add_parser("run", help="MANUAL ONLY: drive the real Runtime API")
    run.add_argument(
        "--base-url", required=True, help="Runtime base URL, e.g. http://127.0.0.1:8000"
    )
    run.add_argument(
        "--workspace",
        default=None,
        help="script-set name prefix; MUST start with ZZ-TEST (default: ZZ-TEST-script-qa-<ts>)",
    )
    run.add_argument(
        "--products",
        default=str(Path(__file__).with_name("fixtures") / "products.json"),
        help="products fixture JSON (default: scripts/qa/fixtures/products.json)",
    )
    run.add_argument("--rounds", type=int, default=3, help="rounds per product (default: 3)")
    run.add_argument("--out", default=None, help="results JSON path")
    run.add_argument("--target-duration-s", type=int, default=240)
    run.add_argument("--poll-interval-s", type=float, default=5.0)
    run.add_argument("--poll-timeout-s", type=float, default=600.0)
    run.add_argument("--regen-attempts", type=int, default=3, help="regenerate attempts per part")
    run.add_argument(
        "--regen-trials",
        type=int,
        default=0,
        help=(
            "after each product's FIRST completed generation, regenerate every segment "
            "round-robin N times sequentially (independent of gate flags; recommend 18 "
            "for the TASK_B §6.7 criterion of >= 18 regens/product, >= 90%% success)"
        ),
    )
    run.add_argument("--only", default=None, help="substring filter on product id")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned API calls without any network call",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "analyze":
        return run_analyze(args)
    return run_real(args)


if __name__ == "__main__":
    sys.exit(main())
