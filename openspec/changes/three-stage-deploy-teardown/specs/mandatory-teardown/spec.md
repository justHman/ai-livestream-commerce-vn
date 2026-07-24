## ADDED Requirements

### Requirement: Iron rule — no idle billable infrastructure
Operators and automation MUST treat billable DEV infrastructure as money-burning while running. Idle ALB, RDS, Redis, ECS tasks, EC2/GPU, or LiveAvatar sessions during code fix, offline debug, or post-smoke wait are forbidden.

#### Scenario: Money rule is explicit in every stage runbook
- **WHEN** a stage runbook is opened
- **THEN** it MUST state the rule "my money is your money"
- **AND** it MUST require teardown before fix work and after success report

### Requirement: Teardown before any debug or fix
If a live stage fails, the stack MUST be fully torn down (preferred) or temporarily stopped with remaining billables listed, before any code change, offline debug, or reconfiguration. Re-deploy is allowed only after the fix is prepared offline.

#### Scenario: Failure forces teardown-first loop
- **WHEN** Stage N smoke or benchmark fails
- **THEN** the operator MUST run full destroy (preferred) and record teardown verification
- **AND** only after teardown verification MAY offline debug/fix begin
- **AND** re-deploy of Stage N MUST follow the offline fix, not precede it

#### Scenario: Temporary stop is second-class and time-bounded
- **WHEN** full destroy is temporarily impossible and temporary stop is used
- **THEN** the record MUST list remaining billable resources (ALB, RDS, Redis, etc.)
- **AND** temporary stop MUST NOT be used as a multi-day holding pattern
- **AND** full destroy MUST still complete before the operator leaves the workday unless the user explicitly extends the window

### Requirement: Teardown after success and report
After a stage passes smoke/benchmark and the stage-exit report is written, the stack MUST be torn down before starting the next stage or ending the session.

#### Scenario: Success still destroys the stack
- **WHEN** Stage N records PASS and a stage-exit report is written
- **THEN** full destroy MUST run before Stage N+1 apply or session end
- **AND** the next stage MUST NOT reuse a still-running previous stack as "already up"

### Requirement: Teardown verification is mandatory evidence
Teardown is incomplete until verification proves expected resources are gone. A destroy command without verification MUST NOT count as teardown complete. Verification MUST also confirm no backup/snapshot leftovers.

#### Scenario: Verify destroy completeness
- **WHEN** full destroy finishes
- **THEN** verification MUST check ECS services/tasks, RDS, ElastiCache, ALB, and unexpected NAT/EC2 resources
- **AND** verification MUST also check for leftover RDS manual/automated snapshots and S3 noncurrent versions
- **AND** the verification result MUST be written to the stage log directory
- **AND** any remaining billable resource or backup leftover MUST be treated as teardown FAIL

### Requirement: No auto-approve live destroy or apply
Live Terraform apply and destroy for billable stages MUST require explicit human approval immediately before execution. Offline plan/validate remains free.

#### Scenario: Apply and destroy stay gated
- **WHEN** an operator reaches the live boundary
- **THEN** commands MUST be presented without `-auto-approve` as the default path
- **AND** documentation MUST require explicit confirmation of account, window, estimated cost, and teardown route before apply or destroy

### Requirement: Iron rule — money-safe boot (desired OFF at apply, scale up after setup)
Every billable stage apply MUST start with all cost-driving desired counts and engine/env flags OFF (`0` / `false` / `none`). The operator MUST complete all setup work that does not need live compute (GitHub Actions image build, S3 weights seed, SSM secrets, config validation, stack health at zero cost) before scaling any cost-driving desired count to its on-state.

#### Scenario: First apply is zero-cost
- **WHEN** a stage stack is first applied
- **THEN** all of `desired_llm_tts`, `desired_avatar`, `desired_livekit`, `desired_lmcache`, `create_ec2_capacity` MUST be `0`/`false`
- **AND** engines MUST be off/mock at first apply
- **AND** the stack MUST be verified healthy at zero compute cost before any scale-up

#### Scenario: Scale up only after setup complete
- **WHEN** the operator wants to run the stage smoke/benchmark
- **THEN** setup MUST already be complete: SHA images pushed by GitHub Actions, weights seeded to S3, SSM secrets present, config validated
- **AND** only then MAY a second apply set the cost-driving desired counts to their on-state (`desired_llm_tts=1`, etc.)
- **AND** the gap between first apply and scale-up MUST NOT incur compute cost for an idle box waiting on setup

#### Scenario: Failure scales back down before fix
- **WHEN** Stage N smoke/benchmark fails
- **THEN** the operator MUST scale cost-driving desired counts back to `0` (or full destroy) before offline fix
- **AND** re-attempt MUST repeat the money-safe boot (zero-cost apply first, then scale up)

### Requirement: Iron rule — no backup storage pile-up
Storage-retention settings MUST keep only the latest data. Old RDS automated backups and old S3 object versions MUST NOT accumulate. Backup hoarding multiplies storage cost and violates the money rule just like idle compute.

#### Scenario: RDS DEV keeps no multi-day backup pile
- **WHEN** a DEV RDS is provisioned for any stage
- **THEN** `backup_retention_days` MUST be `0` (no automated backups) for DEV smoke stacks
- **AND** `skip_final_snapshot` MUST be `true` for DEV so destroy leaves no final snapshot
- **AND** no operator MUST create manual RDS snapshots for DEV stage runs

#### Scenario: S3 keeps current object only
- **WHEN** an S3 bucket is provisioned for DEV assets/weights
- **THEN** `enable_versioning` MUST be `false` for DEV so overwritten uploads do not pile up noncurrent versions
- **AND** if versioning is enabled anywhere (e.g. PROD), `lifecycle_noncurrent_days` MUST expire noncurrent versions to keep current only
- **AND** `force_destroy` MUST be `true` for DEV so teardown empties the bucket without leftovers

#### Scenario: No manual snapshot hoarding across stages
- **WHEN** a stage is torn down
- **THEN** teardown MUST NOT leave behind RDS manual snapshots or S3 staged copies
- **AND** teardown verification MUST list any leftover snapshots / noncurrent S3 versions as teardown FAIL
