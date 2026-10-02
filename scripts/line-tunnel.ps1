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
Push-Location -LiteralPath $repoRoot
try {
    docker compose --profile line up -d line-webhook-proxy line-tunnel | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the LINE tunnel containers' }

    $url = $null
    $deadline = (Get-Date).AddSeconds(90)
    while (-not $url -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        $logs = (docker compose --profile line logs line-tunnel 2>&1 | Out-String)
        $found = [regex]::Matches($logs, 'https://[a-z0-9-]+\.trycloudflare\.com')
        if ($found.Count -gt 0) { $url = $found[$found.Count - 1].Value }
    }
    if (-not $url) { throw 'The tunnel did not report an address within 90 s (docker compose --profile line logs line-tunnel)' }
    Write-Host "Tunnel: $url"

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
    $python | docker compose exec -T -e "WEBHOOK_URL=$url" -w /app app python -
    if ($LASTEXITCODE -ne 0) { throw 'LINE could not reach the webhook through the tunnel' }
    Write-Host 'LINE webhook is set and reachable.'
}
finally {
    Pop-Location
}
