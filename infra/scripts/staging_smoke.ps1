# Stage smoke + teardown-verify helper. Not authorization to apply.
# Usage:
#   infra/scripts/staging_smoke.ps1 -Stage 1 -Base https://<alb> -Token $env:TF_VAR_backend_api_token
#   infra/scripts/staging_smoke.ps1 -Stage 2 -Base ... -Token ... -Sandbox
#   infra/scripts/staging_smoke.ps1 -Stage 3 -Base ... -Token ...
# Writes JSON + SUMMARY.md scaffold into .runtime/stage-{N}-<ts>/.
# Billable: only run against a live stack the operator has explicitly approved.
param(
  [Parameter(Mandatory)][ValidateSet(1,2,3)][int]$Stage,
  [Parameter(Mandatory)][string]$Base,
  [Parameter(Mandatory)][string]$Token,
  [switch]$Sandbox,        # Stage 2: use sandbox avatar for first smoke (no credits)
  [string]$AdminToken      # for /engines endpoint
)
$ErrorActionPreference = "Stop"
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
$logDir = ".runtime/stage-$Stage-$ts"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$h = @{ Authorization = "Bearer $Token"; "Content-Type" = "application/json" }

function Get-Json($url, $name, $headers, $method = "GET", $body = $null) {
  $params = @{ Uri = "$Base$url"; Headers = $headers; Method = $method }
  if ($body) { $params.Body = $body }
  $r = Invoke-RestMethod @params
  $r | ConvertTo-Json -Depth 10 | Set-Content "$logDir/$name.json" -Encoding utf8
  return $r
}

# Health + engines
Get-Json "/api/v1/health/live" "01-live" $h | Out-Null
Get-Json "/api/v1/health/ready" "02-ready" $h | Out-Null
if ($AdminToken) {
  $ah = @{ Authorization = "Bearer $AdminToken"; "Content-Type" = "application/json" }
  Get-Json "/api/v1/engines" "03-engines" $ah | Out-Null
}

# Session lifecycle (POST /api/v1/sessions)
$started = Get-Json "/api/v1/sessions" "04-session-start" $h "POST" "{}"
$sid = $started.session_id
Invoke-RestMethod "$Base/api/v1/sessions/$sid/attach" -Method Post -Headers $h -Body '{"products":[]}' | ConvertTo-Json | Set-Content "$logDir/05-attach.json" -Encoding utf8
Invoke-RestMethod "$Base/api/v1/sessions/$sid/plan/create" -Method Post -Headers $h -Body '{"products":[]}' | ConvertTo-Json | Set-Content "$logDir/06-plan.json" -Encoding utf8
$speakStart = Get-Date
$chatBody = '{"text":"smoke","author":"stage-' + $Stage + '"}'
Invoke-RestMethod "$Base/api/v1/sessions/$sid/chat" -Method Post -Headers $h -Body $chatBody | ConvertTo-Json | Set-Content "$logDir/07-chat.json" -Encoding utf8
$speakMs = [int]((Get-Date) - $speakStart).TotalMilliseconds
Invoke-RestMethod "$Base/api/v1/sessions/$sid/stop" -Method Post -Headers $h -Body "{}" | ConvertTo-Json | Set-Content "$logDir/08-session-stop.json" -Encoding utf8

# Stage 2: assert desired_livekit=0 and sandbox avatar in use; record latency.
# Stage 3: assert self-host avatar path; FE localhost WebRTC check is manual (separate gate).
"stage=$Stage base=$Base speak_ms=$speakMs sandbox=$Sandbox ts=$ts" | Set-Content "$logDir/SUMMARY.md" -Encoding utf8
Write-Host "Smoke captured under $logDir (speak_ms=$speakMs)."
Write-Host "Stage $Stage extra gates:"
if ($Stage -eq 2) { Write-Host "  - verify desired_livekit=0 (no LiveKit bill); sandbox=$Sandbox; LLM/TTS engines real?" }
if ($Stage -eq 3) { Write-Host "  - self_host_avatarforcing_half start/speak/stop; avatar video via LiveKit; then FE localhost WebRTC check (manual)" }
Write-Host "Run scripts/teardown_verify.ps1 after destroy to write teardown-verify.md."
