# GitHub branch rulesets — develop and main

OpenSpec 3.4: protect `develop` and `main` with repository rulesets that require
pull requests, the stable `CI / gate` required check, at least one approval,
resolved conversations, a conflict-free current head, and deny direct
feature-to-`main` or protected-branch pushes (audited emergency bypass only).

This file is the config + admin-apply record. Applying a ruleset requires
repository admin rights; the workflow files and this document are the parts
verifiable in-repo. The exact JSON below can be applied via the GitHub REST
API, the web UI, or the `gh` CLI as documented.

## Rulesets to create

| Ruleset | Enforcement | Target | Purpose |
|---|---|---|---|
| `develop-protection` | Active | `refs/heads/develop` | PR-only integration, stable gate, approval, resolved conversations, current head |
| `main-protection` | Active | `refs/heads/main` | PR-only release, stable gate, approval, resolved conversations, current head; denies feature/* → main direct pushes |

Both rulesets deny direct pushes. Emergency bypass exists only through the
GitHub "bypass" permission granted to repository admins, which GitHub audits
(`gh api /repos/{owner}/{repo}/audit-log`); no workflow-level bypass is
configured.

## Required status check

The single stable required check is:

```text
CI / gate
```

It is produced by `ci.yml` (job `gate`) for every push and pull request, in
every mode (feature-push, feature-pr, develop-merge, release-pr, main-merge).
Unaffected-area jobs report a neutral successful skip so the gate is never
blocked by a job that was not meant to run (3.3).

Note for the admin: when creating the ruleset in the web UI, the required
status check must be added by exact job name `CI / gate`. If the repository
has never run the new `ci.yml` on `develop`/`main` yet, GitHub may not offer
the check in the autocomplete — apply the ruleset via the REST API payload
below (it accepts the exact name) or run one PR merge first so the check name
is registered.

## REST API payload (apply per ruleset)

```json
{
  "name": "develop-protection",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/develop"],
      "exclude": []
    }
  },
  "rules": [
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 1,
        "dismiss_stale_reviews_on_push": true,
        "require_code_owner_review": false,
        "require_last_push_approval": true,
        "required_review_thread_resolution": true
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": false,
        "required_checks": [
          { "context": "CI / gate", "integration_id": null }
        ]
      }
    },
    {
      "type": "required_linear_history",
      "parameters": {}
    },
    {
      "type": "non_fast_forward",
      "parameters": {}
    }
  ],
  "bypass_actors": [
    {
      "actor_id": 5,
      "actor_type": "RepositoryRole",
      "bypass_mode": "always"
    }
  ]
}
```

`main-protection` is identical except:

```json
"name": "main-protection",
"conditions": {
  "ref_name": {
    "include": ["refs/heads/main"],
    "exclude": []
  }
}
```

The `bypass_actors` entry uses GitHub's built-in `Admin` repository role
(`actor_id: 5`), so emergency fixes are possible but appear in the audit log.
Change `bypass_mode` to `pull_request` if you want admins to still need a PR.

## Applying via gh CLI (after admin authentication)

```bash
gh api --method POST repos/{owner}/{repo}/rulesets \
  --input rulesets/develop-protection.json
gh api --method POST repos/{owner}/{repo}/rulesets \
  --input rulesets/main-protection.json
```

The JSON payloads above are stored in-repo under `.github/rulesets/`.

## Verifying the applied ruleset

```bash
gh api repos/{owner}/{repo}/rulesets --jq '.[] | {name, enforcement, target}'
```

Expected: two rulesets, both `active`, `target: branch`. Check the required
status checks on each:

```bash
gh api repos/{owner}/{repo}/rulesets/<id> --jq '.rules[] | select(.type=="required_status_checks")'
```

## Emergency bypass record

Direct push to `develop` or `main` is denied by the ruleset. An audited
emergency bypass is: a repository admin pushes with the bypass permission,
then records the incident (commit SHA, reason, timestamp) in the repo audit
log. The audit log is the record; no code path disables the ruleset.

## What was verified in-repo

- `ci.yml` emits the `CI / gate` job in every mode (static validation +
  actionlint pass).
- `scripts/ci/static_validate_workflows.py` passes on all workflows.
- The actual ruleset creation requires repository admin and is not possible
  from CI or this repo; this document + `.github/rulesets/*.json` are the
  apply-ready artifacts.
