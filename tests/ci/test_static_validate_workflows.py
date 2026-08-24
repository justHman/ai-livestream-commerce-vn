"""Tests for OpenSpec 2.3 static workflow validation."""

from pathlib import Path


from scripts.ci.static_validate_workflows import (
    SERVICE_TAG_PATTERN,
    load_yaml_safe,
    validate_all_workflows,
    validate_trigger_rules,
    validate_reusable_refs,
    validate_no_deploy,
    validate_no_step_level_reusable,
    validate_gh_api_authentication,
    validate_needs_graph,
    validate_cross_job_file_consumption,
    validate_permissions_shape,
    validate_service_tags,
    ValidationResult,
)

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


# ── Fixtures ────────────────────────────────────────────────────────────────


def _result():
    return ValidationResult("fixture.yml")


# ── Loader ──────────────────────────────────────────────────────────────────


def test_loader_keeps_on_key():
    fixture = ROOT / "tests" / "ci" / "fixtures" / "_wellformed_ci.yml"
    doc = load_yaml_safe(fixture)
    assert doc is not None
    assert "on" in doc
    assert "push" in doc["on"]


# ── Trigger rules ───────────────────────────────────────────────────────────


def test_entry_workflow_allows_push_pr_dispatch():
    r = _result()
    validate_trigger_rules({"on": {"push": {"branches": ["main"]}, "pull_request": {}}}, r)
    assert r.passed


def test_unsupported_trigger_rejected():
    r = _result()
    validate_trigger_rules({"on": {"issues": {}}}, r)
    assert not r.passed
    assert any("Unsupported trigger" in e for e in r.errors)


def test_mixed_supported_and_unsupported_rejected():
    r = _result()
    validate_trigger_rules({"on": {"push": {}, "issues": {}}}, r)
    assert not r.passed
    assert any("issues" in e for e in r.errors)


def test_reusable_workflow_requires_workflow_call_only():
    r = _result()
    validate_trigger_rules({"on": {"workflow_call": {}, "push": {}}}, r)
    assert not r.passed
    assert any("workflow_call only" in e for e in r.errors)


def test_reusable_underscore_filename_required():
    r = ValidationResult("not_underscore.yml")
    validate_trigger_rules({"on": {"workflow_call": {}}}, r)
    assert not r.passed
    assert any("leading underscore" in e for e in r.errors)


def test_reusable_no_entry_triggers():
    r = _result()
    validate_trigger_rules({"on": {"workflow_call": {}, "schedule": {}}}, r)
    assert not r.passed
    assert any("entry-only triggers" in e for e in r.errors)


def test_ci_no_workflow_dispatch():
    r = ValidationResult("ci.yml")
    validate_trigger_rules({"on": {"workflow_dispatch": {}}}, r)
    assert not r.passed
    assert any("must not be manually dispatchable" in e for e in r.errors)


def test_entry_workflow_underscore_is_error():
    r = ValidationResult("_bad_entry.yml")
    validate_trigger_rules({"on": {"push": {}}}, r)
    assert not r.passed
    assert any("starts with underscore" in e for e in r.errors)


# ── Reusable refs ───────────────────────────────────────────────────────────


def test_invalid_local_ref_rejected(tmp_path):
    wf_dir = tmp_path
    r = _result()
    validate_reusable_refs(
        {"jobs": {"a": {"uses": "./github/workflows/nope.yml"}}},
        r,
        wf_dir,
    )
    assert not r.passed


def test_missing_local_ref_rejected(tmp_path):
    wf_dir = tmp_path
    r = _result()
    validate_reusable_refs(
        {"jobs": {"a": {"uses": "./.github/workflows/missing.yml"}}},
        r,
        wf_dir,
    )
    assert not r.passed
    assert any("does not exist" in e for e in r.errors)


def test_valid_local_ref_passes(tmp_path):
    (tmp_path / "_reusable.yml").write_text("name: reusable\non:\n  workflow_call:\n")
    r = _result()
    validate_reusable_refs(
        {"jobs": {"a": {"uses": "./.github/workflows/_reusable.yml"}}},
        r,
        tmp_path,
    )
    assert r.passed


# ── Finding 4: local reusable workflow validation + workflow_call secrets ──


def test_local_reusable_deployment_workflow_validates(tmp_path):
    """A local reusable deploy workflow is validated with correct ref form."""
    (tmp_path / "_deploy_reusable.yml").write_text(
        "name: _deploy_reusable\non:\n  workflow_call:\n    inputs:\n      tag:\n        required: true\n        type: string\n    secrets:\n      AWS_ROLE:\n        required: true\n"
    )
    # Validate a job referencing this reusable
    r = ValidationResult("caller.yml")
    validate_reusable_refs(
        {"jobs": {"deploy": {"uses": "./.github/workflows/_deploy_reusable.yml"}}},
        r,
        tmp_path,
    )
    assert r.passed


def test_job_environment_mapping_shapes_validated(tmp_path):
    """Job environment: mapping with name/url is valid; bare number is not."""
    r = _result()
    validate_permissions_shape(
        {"jobs": {"deploy": {"environment": {"name": "production", "url": "https://example.com"}}}},
        r,
    )
    assert r.passed


def test_job_environment_bare_url_mapping_valid(tmp_path):
    r = _result()
    validate_permissions_shape({"jobs": {"deploy": {"environment": "production"}}}, r)
    assert r.passed


def test_workflow_call_secrets_shape_validated():
    """on.workflow_call.secrets must be a mapping with required keys."""
    r = _result()
    validate_permissions_shape(
        {"on": {"workflow_call": {"secrets": {"TOKEN": {"required": True}}}}, "jobs": {}}, r
    )
    assert r.passed


def test_workflow_call_secrets_invalid_shape_rejected():
    """on.workflow_call.secrets as a string is invalid."""
    r = ValidationResult("_reusable.yml")
    validate_permissions_shape(
        {"on": {"workflow_call": {"secrets": "SHARED_TOKEN"}}, "jobs": {}}, r
    )
    assert not r.passed
    assert any("on.workflow_call.secrets" in e for e in r.errors)


# ── R1.1: reusable workflows never invoked via steps[*].uses ───────────────


def test_step_level_local_reusable_rejected():
    r = _result()
    validate_no_step_level_reusable(
        {
            "jobs": {
                "deploy": {
                    "steps": [
                        {"uses": "actions/checkout@v4"},
                        {"uses": "./.github/workflows/_deploy-service.yml"},
                    ]
                }
            }
        },
        r,
    )
    assert not r.passed
    assert any("steps[].uses" in e and "_deploy-service.yml" in e for e in r.errors)


def test_step_level_external_action_allowed():
    r = _result()
    validate_no_step_level_reusable(
        {
            "jobs": {
                "build": {
                    "steps": [
                        {"uses": "actions/checkout@v4"},
                        {"uses": "docker/build-push-action@v6"},
                    ]
                }
            }
        },
        r,
    )
    assert r.passed


def test_job_level_local_reusable_allowed():
    r = _result()
    validate_no_step_level_reusable(
        {
            "jobs": {
                "build": {
                    "uses": "./.github/workflows/_container-build.yml",
                    "with": {"image": "x"},
                    "steps": [{"uses": "actions/checkout@v4"}],
                }
            }
        },
        r,
    )
    assert r.passed


# ── R1.2: gh api authentication ─────────────────────────────────────────────


def test_gh_api_missing_token_rejected():
    r = _result()
    validate_gh_api_authentication(
        {"jobs": {"v": {"steps": [{"run": 'conclusion=$(gh api "repos/x/runs" --jq y)'}]}}},
        r,
    )
    assert not r.passed
    assert any("GH_TOKEN" in e and "gh api" in e for e in r.errors)


def test_gh_api_with_token_passes():
    r = _result()
    validate_gh_api_authentication(
        {
            "jobs": {
                "v": {
                    "steps": [
                        {
                            "run": 'conclusion=$(gh api "repos/x/runs" --jq y)',
                            "env": {"GH_TOKEN": "${{ github.token }}"},
                        }
                    ]
                }
            }
        },
        r,
    )
    assert r.passed


def test_gh_api_suppression_rejected_even_with_token():
    r = _result()
    validate_gh_api_authentication(
        {
            "jobs": {
                "v": {
                    "steps": [
                        {
                            "run": 'gh api "repos/x/env" --jq y 2>/dev/null || true',
                            "env": {"GH_TOKEN": "${{ github.token }}"},
                        }
                    ]
                }
            }
        },
        r,
    )
    assert not r.passed
    assert any("suppresses" in e for e in r.errors)


def test_gh_api_multiline_suppression_rejected():
    r = _result()
    validate_gh_api_authentication(
        {
            "jobs": {
                "v": {
                    "steps": [
                        {
                            "run": (
                                'conclusion=$(gh api "repos/x/runs?head_sha=a" \\\n'
                                '  --jq y 2>/dev/null || true)'
                            ),
                            "env": {"GH_TOKEN": "${{ github.token }}"},
                        }
                    ]
                }
            }
        },
        r,
    )
    assert not r.passed
    assert any("suppresses" in e for e in r.errors)


# ── R1.7: needs graph directness ────────────────────────────────────────────


def test_indirect_needs_output_rejected():
    r = _result()
    validate_needs_graph(
        {
            "jobs": {
                "a": {"runs-on": "x", "steps": [{"run": "echo"}]},
                "b": {"needs": ["a"], "runs-on": "x", "steps": [{"run": "echo"}]},
                "c": {
                    "needs": ["b"],
                    "env": {"S": "${{ needs.a.outputs.validated_sha }}"},
                    "runs-on": "x",
                    "steps": [{"run": "echo $S"}],
                },
            }
        },
        r,
    )
    assert not r.passed
    assert any("not a direct declared dependency" in e and "needs.a" in e for e in r.errors)


def test_direct_needs_output_accepted():
    r = _result()
    validate_needs_graph(
        {
            "jobs": {
                "a": {"runs-on": "x", "outputs": {"s": "x"}, "steps": [{"run": "echo"}]},
                "b": {
                    "needs": ["a"],
                    "env": {"S": "${{ needs.a.outputs.s }}"},
                    "runs-on": "x",
                    "steps": [{"run": "echo $S"}],
                },
            }
        },
        r,
    )
    assert r.passed


# ── R1.6: cross-job file consumption ────────────────────────────────────────


def test_cross_job_runtime_read_without_transport_rejected():
    r = _result()
    validate_cross_job_file_consumption(
        {
            "jobs": {
                "read": {
                    "runs-on": "x",
                    "steps": [{"run": 'jq -r . <<<"$(cat .runtime/deploy/evidence/staging/a.jsonl)"'}],
                }
            }
        },
        r,
    )
    assert not r.passed
    assert any("R1.6" in e and ".runtime/" in e for e in r.errors)


def test_cross_job_runtime_read_with_artifact_download_accepted():
    r = _result()
    validate_cross_job_file_consumption(
        {
            "jobs": {
                "read": {
                    "runs-on": "x",
                    "steps": [
                        {"uses": "actions/download-artifact@v4"},
                        {"run": "cat .runtime/deploy/evidence/staging/a.jsonl"},
                    ],
                }
            }
        },
        r,
    )
    assert r.passed


def test_same_job_runtime_write_and_read_accepted():
    r = _result()
    validate_cross_job_file_consumption(
        {
            "jobs": {
                "w": {
                    "runs-on": "x",
                    "steps": [
                        {"run": 'mkdir -p .runtime/ev && echo x >> .runtime/ev/a.jsonl'},
                        {"run": "cat .runtime/ev/a.jsonl"},
                    ],
                }
            }
        },
        r,
    )
    assert r.passed


# ── Service tags ────────────────────────────────────────────────────────────


def test_service_tag_pattern_exact():
    assert SERVICE_TAG_PATTERN.match("backend-v1.2.3")
    assert SERVICE_TAG_PATTERN.match("llm-v0.1.0")
    assert SERVICE_TAG_PATTERN.match("avatar-v2.0.0")
    assert not SERVICE_TAG_PATTERN.match("v1.2.3")
    assert not SERVICE_TAG_PATTERN.match("backend-v1.2")
    assert not SERVICE_TAG_PATTERN.match("backend-1.2.3")


def test_unsupported_service_tag_is_error():
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["database-v1.2.3"]}}}, r)
    assert not r.passed
    assert any("unsupported service" in e for e in r.errors)


def test_malformed_semver_tag_is_error():
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["backend-v1.2"]}}}, r)
    assert not r.passed
    assert any("valid release tag" in e for e in r.errors)


def test_broad_v_star_tag_allowed():
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["v*"]}}}, r)
    assert r.passed


def test_canonical_service_tags_pass():
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["backend-v1.2.3", "tts-v0.4.5"]}}}, r)
    assert r.passed


# ── Finding 3: malformed service-shaped tags ───────────────────────────────


def test_service_tag_missing_v_prefix_rejected():
    """backend-1.2.3 is service-shaped but missing v prefix — reject."""
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["backend-1.2.3"]}}}, r)
    assert not r.passed
    assert any("valid release tag" in e for e in r.errors)


def test_service_tag_wildcard_like_rejected():
    """backend-* is service-shaped wildcard — reject."""
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["backend-*"]}}}, r)
    assert not r.passed
    assert any("valid release tag" in e for e in r.errors)


def test_broad_v_star_allowed_with_service_shaped_tags():
    """v* remains allowed alongside service-shaped tags — only the malformed one errors."""
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["v*", "backend-v1.2.3"]}}}, r)
    assert r.passed


def test_service_tag_patch_missing_rejected():
    """backend-v1.2 is missing patch — reject."""
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["backend-v1.2"]}}}, r)
    assert not r.passed
    assert any("valid release tag" in e for e in r.errors)


def test_service_tag_unknown_service_rejected():
    """backendx-v1.2.3 uses short name not in SERVICE_TAG_NAMES — reject."""
    r = _result()
    validate_service_tags({"on": {"push": {"tags": ["backendx-v1.2.3"]}}}, r)
    assert not r.passed
    assert any("unsupported service" in e for e in r.errors)


# ── CI no deploy ────────────────────────────────────────────────────────────


def test_ci_no_deploy_step():
    r = ValidationResult("ci.yml")
    validate_no_deploy(
        {
            "jobs": {
                "deploy": {"steps": [{"run": "aws ecs update-service --cluster x --service y"}]}
            }
        },
        r,
    )
    assert not r.passed
    assert any("deployment command" in e for e in r.errors)


def test_ci_deploy_action_job_uses_rejected():
    r = ValidationResult("ci.yml")
    validate_no_deploy(
        {"jobs": {"deploy": {"uses": "aws-actions/amazon-ecs-deploy-task-definition@v2"}}},
        r,
    )
    assert not r.passed
    assert any("deploy action" in e for e in r.errors)


def test_ci_deploy_action_step_uses_rejected():
    r = ValidationResult("ci.yml")
    validate_no_deploy(
        {
            "jobs": {
                "deploy": {"steps": [{"uses": "aws-actions/amazon-ecs-deploy-task-definition@v2"}]}
            }
        },
        r,
    )
    assert not r.passed


def test_ci_without_deploy_step_passes():
    r = ValidationResult("ci.yml")
    validate_no_deploy({"jobs": {"lint": {"steps": [{"run": "uvx ruff check ."}]}}}, r)
    assert r.passed


# ── Permissions / environment / secrets ─────────────────────────────────────


def test_deploy_workflow_requires_permissions():
    r = ValidationResult("deploy-dev.yml")
    validate_permissions_shape({"jobs": {"deploy": {}}}, r)
    assert not r.passed
    assert any("permissions" in e for e in r.errors)


def test_deploy_workflow_valid_permissions_passes():
    r = ValidationResult("deploy-dev.yml")
    validate_permissions_shape(
        {"permissions": {"id-token": "write", "contents": "read"}, "jobs": {}}, r
    )
    assert r.passed


def test_invalid_environment_reference_rejected():
    r = _result()
    validate_permissions_shape({"jobs": {"deploy": {"environment": 123}}}, r)
    assert not r.passed
    assert any("environment reference" in e for e in r.errors)


def test_valid_environment_mapping_passes():
    r = _result()
    validate_permissions_shape({"jobs": {"deploy": {"environment": {"name": "production"}}}}, r)
    assert r.passed


def test_invalid_secrets_shape_rejected():
    r = _result()
    validate_permissions_shape({"jobs": {"deploy": {"secrets": "SECRET_TOKEN"}}}, r)
    assert not r.passed
    assert any("secrets block" in e for e in r.errors)


def test_valid_secrets_mapping_passes():
    r = _result()
    validate_permissions_shape(
        {"jobs": {"deploy": {"secrets": {"TOKEN": "${{ secrets.TOKEN }}"}}}}, r
    )
    assert r.passed


# ── Repo-wide ───────────────────────────────────────────────────────────────


def test_all_repo_workflows_pass():
    results = validate_all_workflows(WORKFLOWS)
    for r in results:
        assert r.passed, f"{r.workflow_path}: {r.errors}"
