# Infra validation — 6.6 evidence (global/dev/staging/prod)

Run 2026-08-05, terraform v1.14.8, repo base c635f4f+ (cluster 6.x). All
commands offline (no AWS credentials needed for format/init/validate/tests).

## Per-environment results

| Env | fmt -check | init -backend=false | validate | State key |
|---|---|---|---|---|
| global | PASS | PASS | PASS | ai-livestream-tfstate / global/terraform.tfstate (use_lockfile=true) |
| dev | PASS | PASS | PASS | ai-livestream-tfstate / dev/terraform.tfstate (use_lockfile=true) |
| staging | PASS | PASS | PASS | ai-livestream-tfstate / staging/terraform.tfstate (use_lockfile=true) |
| prod | PASS | PASS | PASS | ai-livestream-tfstate / prod/terraform.tfstate (use_lockfile=true) |

Recursive format: `terraform fmt -check -recursive infra` → exit 0 (all files
formatted). No environment uses workspaces; all four backends use the native
S3 lockfile (`use_lockfile = true`), no DynamoDB lock table, and each state
key is distinct (verified by `scripts/ci/check_infra_module_boundaries.py`
rules 4).

## Native tests (infra/tests)

`terraform test -filter=runtime_matrix.tftest.hcl` (infra/tests, init
-backend=false):

| Run | Result |
|---|---|
| zero_cost_hosted_topology | PASS — no ASG/ECS llm, no migrate task when hosted adapters + no DATABASE_URL |
| independent_llm_tts | PASS — llm/tts never share a task definition |
| no_forbidden_topology | PASS — no standalone LMCache / LiveKit / internal NLB |
| digest_and_circuit_breaker | PASS — backend image is @sha256:, circuit breaker rollback=true |

Python infra tests: `pytest infra/tests/` → 16 passed (module source
resolution, DATABASE_URL secret wiring, platform roots + build-images ref).

## Static boundary + invariant checks

`python scripts/ci/check_infra_module_boundaries.py` → OK (8 canonical
modules; no forbidden service-named modules; no combined llm_tts blocks; no
standalone LMCache/LiveKit; no LB outside loadbalancer module; prod backend
FARGATE-only + desired_backend ≥ 1; create_rds/create_redis defaults
false/true/true; backend receives only *_ADAPTER; immutable digests in all
tfvars examples; every ECS service has a deployment circuit breaker; no
plaintext token variables; compose.yaml local-only with data/media profiles).

## Cloud Map discovery

`infra/modules/compute/discovery.tf` — one private DNS namespace
`<env>.ai-live.local` + one SD service per model service (llm, tts), each
MULTIVALUE A record, gated on `create_ec2_capacity`. LLM/TTS ECS services
attach `registry_arn` (llm.tf:512, tts.tf:77). Backend task receives
`LLM_BASE_URL`/`TTS_BASE_URL` defaulting to
`http://llm.<env>.ai-live.local:8001` / `http://tts.<env>.ai-live.local:8002`
(backend.tf:36,44). The internal model NLB was removed (1.66); `moved` blocks
in llm.tf migrate the pre-split `llm_tts` addresses to `llm`. No
`aws_lb` resource exists in the compute module (boundary check rule 8).

## Secret / state inspection

- No plaintext token variables anywhere (rule 13); dev tfvars.example passes
  only image digests and non-secret values; runtime secrets are SSM
  SecureString ARNs passed as `secrets_arns` (backend/database_url via
  `database_url_parameter_arn`, liveavatar/llm/tts api_key parameter ARNs).
- Comment cleanup: two stale `llm_engine=openai_compat` references in
  infra/environments/dev/main.tf and infra/modules/compute/backend.tf were
  legacy-selector vocabulary (removed by 1.33); rewording only, no resource
  change. Re-validated dev after edit.
- No `terraform_remote_state` / cross-state references (rule 4).
- OIDC/IAM narrowing is documented in infra/environments/global/main.tf;
  deploy roles lack ssm:PutParameter (out-of-band provisioning per 4.x report).

## Not run locally (require AWS credentials / GitHub admin — admin-apply list)

- Non-production apply/teardown drills (infra-apply/infra-teardown against
  live dev/staging stacks, trusted plan + typed confirmation).
- Immutable-digest rollout and circuit-breaker rollback against live ECS.
- Cloud Map DNS resolution against a live environment.
- These are operator/admin steps; the workflow files (infra-apply.yml,
  infra-teardown-nonprod.yml) and their protection config are ready per the
  4.x cluster report admin-apply list.
