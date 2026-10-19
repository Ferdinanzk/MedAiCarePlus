<#
Start the LINE webhook tunnel and point the LINE channel's webhook at it.

The Cloudflare quick tunnel gets a new address every time it starts (laptop restart, Docker restart), so run
this script again whenever LINE verification or caregiver buttons stop working. Only POST
/api/notify/webhook/line is reachable through the tunnel (scripts/line-webhook/nginx.conf).

    & "C:\medcareai\MedAiCarePlus\scripts\line-tunnel.ps1"
#>
# Docker writes progress to stderr; Windows PowerShell 5.1 would turn that into errors under 'Stop'.
# Native exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
# Written on success: the tunnel container's StartedAt and the address registered with LINE. medcare-boot.ps1
# -Reconcile reruns this script when the container's StartedAt no longer matches it (the tunnel restarted).
$stampFile = Join-Path $repoRoot 'logs\line-tunnel.stamp'

function Get-TunnelStartedAt {
    $id = (docker compose --profile line ps -q line-tunnel 2>$null | Out-String).Trim()
    if (-not $id) { return $null }
    return (docker inspect -f '{{.State.StartedAt}}' $id 2>$null | Out-String).Trim()
}

function Wait-TunnelUrl([string]$StartedAt) {
    # Only the log lines of the current start: after an engine restart the log also holds earlier runs' addresses.
    $deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        $logs = (docker compose --profile line logs --since $StartedAt line-tunnel 2>&1 | Out-String)
        $found = [regex]::Matches($logs, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($found.Count -gt 0) { return $found[$found.Count - 1].Value }
    }
    return $null
}

Push-Location -LiteralPath $repoRoot
try {
    # --no-deps: a changed postgres or app definition must never recreate them from here (reconcile runs this
    # whatever a dose session is doing); scripts\rebuild-app.ps1 is the guarded way to apply it.
    docker compose --profile line up -d --no-deps line-webhook-proxy line-tunnel | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the LINE tunnel containers' }

    $python = @'
import json, os, sys, time, urllib.error, urllib.request
from app import config

endpoint = os.environ["WEBHOOK_URL"].rstrip("/") + "/api/notify/webhook/line"
headers = {"Authorization": "Bearer " + config.LINE_CHANNEL_ACCESS_TOKEN, "Content-Type": "application/json"}

def call(method, path, body):
    req = urllib.request.Request("https://api.line.me/v2/bot/channel/webhook/" + path, method=method,
                                 data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")

if not config.LINE_CHANNEL_ACCESS_TOKEN:
    sys.exit("LINE_CHANNEL_ACCESS_TOKEN is empty in .env")
# A new trycloudflare name can take a while to resolve, and LINE rejects an endpoint it can't resolve yet
# ("Invalid webhook endpoint URL"), so both calls are retried.
for attempt in range(12):
    status, body = call("PUT", "endpoint", {"endpoint": endpoint})
    if status == 200:
        break
    time.sleep(5)
print("set webhook:", status, body or "ok", endpoint)
if status != 200:
    sys.exit("LINE refused the webhook address")
for attempt in range(6):
    status, body = call("POST", "test", {"endpoint": endpoint})
    if body.get("success"):
        break
    time.sleep(5)
print("LINE test:", status, body)
req = urllib.request.Request("https://api.line.me/v2/bot/channel/webhook/endpoint", headers=headers)
with urllib.request.urlopen(req, timeout=20) as resp:
    current = json.loads(resp.read())
print("LINE now sends to:", current.get("endpoint"), "| active:", current.get("active"))
sys.exit(0 if body.get("success") and current.get("endpoint") == endpoint else 1)
'@
    # A tunnel that is up but dead (LINE's test fails) gets one fresh start: a new container, a new address.
    for ($round = 1; $round -le 2; $round++) {
        $started = Get-TunnelStartedAt
        if (-not $started) { throw 'The line-tunnel container is not there (docker compose --profile line ps)' }
        $url = Wait-TunnelUrl $started
        if (-not $url) { throw 'The tunnel did not report an address within 90 s (docker compose --profile line logs line-tunnel)' }
        Write-Host "Tunnel: $url"
        $python | docker compose exec -T -e "WEBHOOK_URL=$url" -w /app app python -
        if ($LASTEXITCODE -eq 0) {
            New-Item -ItemType Directory -Force -Path (Split-Path $stampFile -Parent) | Out-Null
            Set-Content -LiteralPath $stampFile -Value @("started_at=$started", "address=$url") -Encoding ASCII
            Write-Host 'LINE webhook is set and reachable.'
            return
        }
        if ($round -eq 1) {
            Write-Host 'LINE could not reach the webhook through the tunnel: recreating the tunnel container once'
            docker compose --profile line up -d --no-deps --force-recreate line-tunnel | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Could not recreate the LINE tunnel container' }
        }
    }
    throw 'LINE could not reach the webhook through the tunnel, also after a fresh tunnel'
}
finally {
    Pop-Location
}
