# GitHub Secret Scanning and Push Protection — enablement record

OpenSpec 3.7: enable and verify GitHub Secret Scanning and Push Protection
when the repository entitlement supports them, without weakening the Gitleaks
required check (`CI / gate`). Gitleaks remains the portable, repo-owned gate;
Secret Scanning is an additional prevention layer.

Enabling Secret Scanning / Push Protection requires repository admin rights
(and, for Push Protection, an organization plan that supports it). This file
records what to apply and how to verify it. The in-repo verifiable parts are
the Gitleaks config (` .gitleaks.toml`) and the `secret-scan` job in
`ci.yml`.

## Enable Secret Scanning (admin action)

Web UI: Settings → Code security and analysis → Secret scanning → Enable.
Push Protection: Settings → Code security and analysis → Secret scanning →
click the "Push protection" toggle (requires Secret Scanning enabled first).

CLI equivalent (admin token):

```bash
gh api --method PATCH repos/{owner}/{repo} \
  -f security_and_analysis[secret_scanning][status]=enabled
# Push Protection is org-level for private repos; on GitHub.com push
# protection is on by default for public repos and can be toggled per repo
# via the UI once Secret Scanning is enabled.
```

## Verify (post-enable)

```bash
gh api repos/{owner}/{repo} \
  --jq '.security_and_analysis.secret_scanning.status'
# expected: "enabled"
```

Secret Scanning runs on GitHub's side over pushed commits; its alerts appear
under the repository **Security** tab, not in Actions. It is a detection
layer, not a CI check. Push Protection blocks commits containing known secret
patterns at push time.

## Why Gitleaks stays mandatory (do not weaken)

- Secret Scanning only knows GitHub's curated pattern set; Gitleaks covers
  project-specific and custom patterns via `.gitleaks.toml`.
- The `CI / gate` required check (rulesets, 3.4) depends on the Gitleaks
  `secret-scan` job. Removing or weakening it would break the stable gate.
- Secret Scanning does not run for repositories on free plans or without
  admin enablement; Gitleaks is entitlement-independent.

## In-repo verified state (2026-08-05)

- `.gitleaks.toml` present with the pre-existing Workbench dev-token allowlist
  (1.47); unchanged.
- `ci.yml` `secret-scan` job pinned to `gitleaks/gitleaks-action@v2.3.6` with
  `GITLEAKS_VERSION=8.24.3`; redacted output (`--redact` built into the
  action), fails the job on findings, feeds `CI / gate`.
- Initial full-history scan (gitleaks 8.24.3, 330 commits): no leaks. No
  allowlist additions needed (3.6).
- Version pin rationale: `gitleaks-action@v3` requires a `GITLEAKS_LICENSE`
  secret for ALL repositories; v2.3.6 only enforces it for Organization-owned
  repos. This repo is user-owned, so v2.3.6 runs without a license. If the
  repository moves to an Organization account, set the `GITLEAKS_LICENSE`
  secret and bump to v3.
