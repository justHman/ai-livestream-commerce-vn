# deploy.ps1 — dispatch dev/staging deployment via gh CLI (OpenSpec 4.3).
# Usage: scripts/deploy.ps1 -Env dev -Sha <full-40-hex> -Services backend_service,tts_service [-Watch]
param(
  [Parameter(Mandatory)][ValidateSet("dev", "staging")][string]$Env,
  [Parameter(Mandatory)][string]$Sha,
  [Parameter(Mandatory)][string]$Services,
  [switch]$Watch
)

$ErrorActionPreference = "Stop"

if ($Sha -notmatch '^[0-9a-f]{40}$') {
  Write-Error "SHA must be a full 40-hex commit SHA."
  exit 1
}
if ([string]::IsNullOrWhiteSpace($Services)) {
  Write-Error "Services list must not be empty (e.g. backend_service,tts_service)."
  exit 1
}

$null = gh auth status 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Error "gh is not authenticated. Run: gh auth login"
  exit 1
}

$workflow = if ($Env -eq "dev") { "deploy-dev.yml" } else { "deploy-staging.yml" }
$ref = if ($Env -eq "dev") { "develop" } else { "main" }

gh workflow run $workflow --ref $ref -f "commit_sha=$Sha" -f "services=$Services"
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$runUrl = gh run list --workflow $workflow -L 1 --json url --jq '.[0].url'
Write-Host "Dispatched $workflow (ref=$ref) for $Sha"
Write-Host "Run: $runUrl"

if ($Watch) {
  gh run watch --exit-status
  if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
