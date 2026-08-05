# Production release — service tags, approval, digest promotion

Production releases are service-scoped and digest-exact (OpenSpec 5.x).
`release-service.yml` runs when a tag of the form `<service>-vMAJOR.MINOR.PATCH`
is pushed (e.g. `backend-v1.2.0`, `avatar-v0.5.0`; supported services:
`backend`, `llm`, `tts`, `avatar`).

## Release flow

1. **Staging first.** Deploy the commit to staging
   (`gh workflow run deploy-staging.yml --ref main -f commit_sha=<sha> -f services=...`,
   see [deploy-commands.md](./deploy-commands.md)) and confirm the smoke
   passes. The workflow commits `deploy-evidence/staging/<sha>.jsonl` to main
   with the exact image digests.
2. **Tag the release.**
   ```bash
   git tag backend-v1.2.0 <sha>     # sha must be contained in main
   git push origin backend-v1.2.0
   ```
3. **Validation (5.2).** `release-service.yml` parses the tag, rejects it if
   the tag commit is not contained in `main`, and rejects it if there is no
   successful staging evidence line for that service and commit. No staging
   evidence, no release.
4. **Approval (5.3).** The deploy job targets the protected `production`
   environment. A required-reviewers approval gate must be configured on it
   (admin setup below); the workflow additionally verifies at runtime that
   `required_reviewers` protection exists and fails closed if it does not —
   production stays blocked rather than silently bypassing approval.
5. **Promotion (5.4).** After approval, the EXACT staging-verified image
   digest is promoted. Nothing is rebuilt: the workflow verifies the digest
   exists in Docker Hub, then deploys that digest.
6. **Smoke + rollback (5.5).** The backend is smoke-checked via
   `/api/v1/health/live`. On any failure the shared deploy step restores ONLY
   the affected service to its previous task definition (previous digest);
   other services are untouched.

## Admin setup (one-time, repository admin)

- Create/verify the GitHub Environment `production` (Settings → Environments):
  - Add `Required reviewers` — the release approver, distinct from the
    operator who pushes tags. GitHub has no per-environment self-approval
    toggle; requiring a reviewer different from the releasing actor is how
    self-approval is prevented. Repository admins can still bypass environment
    protection — treat that as the audited exception path.
  - Add the environment secrets `AWS_ROLE_ARN_PROD`, `DOCKERHUB_USER`,
    `DOCKERHUB_TOKEN`.
- `AWS_ROLE_ARN_PROD`: the `ai-livestream-github-deploy-prod` role from
  `infra/environments/global` (apply with `github_environment = "prod"`).
  The OIDC trust condition binds the role to this repository + the
  `production` environment.
- `deploy-prod.yml` is superseded by `release-service.yml`. Its triggers are
  disabled by task 6.4 once the replacement paths pass non-production
  verification — do not rely on it for new releases.

## Failure semantics

- Tag invalid / commit not in main / no staging evidence → run fails before
  any AWS action; nothing changes in production.
- Approval not configured → run fails at the readiness check (5.3).
- Digest missing in Docker Hub → run fails before any ECS update.
- Deployment or smoke failure → only the affected service is rolled back to
  its previous digest; the run reports failure.

Release outcomes are recorded under
`.runtime/deploy/evidence/prod/<sha>.jsonl` (audit trail, same schema as 4.4).
