# Teardown verification — mandatory evidence after destroy (or temporary stop).
# Iron rule: a destroy command without verification does NOT count as teardown complete.
# Checks ECS, RDS (+ leftover snapshots), ElastiCache, ALB, S3 noncurrent versions,
# EC2, NAT. Writes teardown-verify.md into the stage log dir.
# Usage:
#   scripts/teardown_verify.ps1 -LogDir .runtime/stage-2-20260724-153000 -Env dev
param(
  [Parameter(Mandatory)][string]$LogDir,
  [string]$Env = "dev",
  [string]$Project = "ai-livestream",
  [string]$Region = "ap-northeast-2"
)
$ErrorActionPreference = "Stop"
$env:AWS_REGION = $Region
$prefix = "$Project-$Env"
$report = "$LogDir/teardown-verify.md"
"date=$([DateTime]::Now.ToString('yyyy-MM-dd HH:mm')) env=$Env project=$Project" | Set-Content $report -Encoding utf8

function Count-Or-Empty($cmd) {
  $out = try { & $cmd 2>$null } catch { "" }
  $count = ($out | Where-Object { $_ -ne "" } | Measure-Object).Count
  return if ($null -ne $count -and $out) { $count } else { 0 }
}

function Add-Line($label, $value) {
  Add-Content -Path $report -Value "- $label`: $value" -Encoding utf8
}

# ECS RUNNING tasks across all clusters (dev env)
$clusters = aws ecs list-clusters --query "clusterArns[?contains(@,'$prefix')]" --output text 2>$null
$running = 0
foreach ($c in ($clusters -split "\s+" | Where-Object { $_ })) {
  $tasks = aws ecs list-tasks --cluster $c --desired-status RUNNING --query "taskArns" --output text 2>$null
  $running += ($tasks | Where-Object { $_ } | Measure-Object).Count
}
Add-Line "ECS RUNNING tasks" $running

# RDS
$rds = aws rds describe-db-instances --query "DBInstances[?contains(DBInstanceIdentifier,'$prefix')].DBInstanceIdentifier" --output text 2>$null
Add-Line "RDS instances" (($rds | Where-Object { $_ } | Measure-Object).Count)
$snaps = aws rds describe-db-snapshots --query "DBSnapshots[?contains(DBSnapshotIdentifier,'$prefix')].DBSnapshotIdentifier" --output text 2>$null
Add-Line "RDS snapshots (manual+automated, env-scoped)" (($snaps | Where-Object { $_ } | Measure-Object).Count)

# ElastiCache
$ec = aws elasticache describe-cache-clusters --query "CacheClusters[?contains(CacheClusterId,'$prefix')].CacheClusterId" --output text 2>$null
Add-Line "ElastiCache clusters" (($ec | Where-Object { $_ } | Measure-Object).Count)

# ALB
$alb = aws elbv2 describe-load-balancers --query "LoadBalancers[?starts_with(LoadBalancerName,'$prefix')].LoadBalancerName" --output text 2>$null
Add-Line "ALB (env-scoped)" (($alb | Where-Object { $_ } | Measure-Object).Count)

# S3 noncurrent versions on the assets bucket (DEV versioning off -> 0 expected)
$bucket = "$prefix-assets-191918535424"
$versions = aws s3api list-object-versions --bucket $bucket --query "Versions[].VersionId" --output text 2>$null
$delMarkers = aws s3api list-object-versions --bucket $bucket --query "DeleteMarkers[].VersionId" --output text 2>$null
$nc = 0
foreach ($v in ($versions -split "\s+" | Where-Object { $_ })) { $nc++ }
Add-Line "S3 noncurrent versions ($bucket)" $nc
$dm = 0
foreach ($d in ($delMarkers -split "\s+" | Where-Object { $_ })) { $dm++ }
Add-Line "S3 delete markers ($bucket)" $dm

# EC2 + NAT
$ec2 = aws ec2 describe-instances --filters "Name=tag:Env,Values=$Env" --query "Reservations[].Instances[].InstanceId" --output text 2>$null
Add-Line "EC2 instances (env-tagged)" (($ec2 | Where-Object { $_ } | Measure-Object).Count)
$nat = aws ec2 describe-nat-gateways --query "NatGateways[].NatGatewayId" --output text 2>$null
Add-Line "NAT gateways" (($nat | Where-Object { $_ } | Measure-Object).Count)

# Verdict
$fail = $running -gt 0 -or ($rds | Where-Object { $_ } | Measure-Object).Count -gt 0 -or ($snaps | Where-Object { $_ } | Measure-Object).Count -gt 0 -or ($ec | Where-Object { $_ } | Measure-Object).Count -gt 0 -or ($alb | Where-Object { $_ } | Measure-Object).Count -gt 0 -or $nc -gt 0 -or ($ec2 | Where-Object { $_ } | Measure-Object).Count -gt 0 -or ($nat | Where-Object { $_ } | Measure-Object).Count -gt 0
$verdict = if ($fail) { "TEARDOWN_FAIL" } else { "TEARDOWN_VERIFIED" }
Add-Line "Verdict" $verdict
Write-Host "Wrote $report — verdict: $verdict"
if ($fail) { exit 1 }
