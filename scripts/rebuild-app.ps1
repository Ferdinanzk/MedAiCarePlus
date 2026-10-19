<#
Rebuild and restart the app container, but only while no dose session is in progress (19 Oct 2026).

A rebuild takes the server away for about 14 s, longer than the robot's 10 s limit: a dose slot in progress is
dropped, and when the robot picks the task up again it announces the dose again and no longer waits for family's
answer. So this refuses while a robot task is leased, searching or in progress (and not expired), or while a dose
confirmation still waits for family's answer (resolution IS NULL).

    .\scripts\rebuild-app.ps1          # refuses with the reason while a session is active, else rebuilds the app
    .\scripts\rebuild-app.ps1 -Force   # rebuilds anyway

Windows PowerShell 5.1.
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [string]$RepoRoot = '',
    [string]$ComposeProject = 'medcareai2'
)
if (-not $RepoRoot) { $RepoRoot = Split-Path $PSScriptRoot -Parent }
$ErrorActionPreference = 'Continue'
$composeFile = Join-Path $RepoRoot 'docker-compose.yml'

function Get-Count([string]$Sql) {
    # One count from the database, or $null when postgres does not answer.
    $out = (& docker compose -p $ComposeProject -f $composeFile exec -T postgres psql -U medai -d medcareai2 -Atc $Sql 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $out -notmatch '^\d+$') { return $null }
    return [int]$out
}

function Get-BusyReasons($Tasks, $OpenConfirmations) {
    # Why a rebuild now would break a session; empty when it is safe.
    $reasons = @()
    if ($null -eq $Tasks -or $null -eq $OpenConfirmations) {
        $reasons += 'the database did not answer, so it is unknown whether a dose session is running (is postgres up?)'
    } else {
        if ($Tasks -gt 0) { $reasons += "$Tasks robot task(s) are leased, searching or in progress" }
        if ($OpenConfirmations -gt 0) { $reasons += "$OpenConfirmations dose confirmation(s) still wait for family's answer" }
    }
    return ,$reasons
}

$tasks = Get-Count "SELECT count(*) FROM reachy_task WHERE status IN ('leased','searching','in_progress') AND expires_at > NOW()"
$open = Get-Count 'SELECT count(*) FROM dose_confirmation WHERE resolution IS NULL'
$reasons = Get-BusyReasons $tasks $open
if ($reasons.Count -gt 0 -and -not $Force) {
    Write-Host ("Not rebuilding: {0}." -f ($reasons -join '; '))
    Write-Host 'A rebuild stops the server for about 14 s, and the robot drops a dose session after 10 s. Try again when it is over, or run with -Force.'
    exit 1
}
if ($reasons.Count -gt 0) { Write-Host ("Rebuilding anyway (-Force): {0}." -f ($reasons -join '; ')) }
else { Write-Host 'No dose session is running: rebuilding the app.' }
& docker compose -p $ComposeProject -f $composeFile up -d --build app
exit $LASTEXITCODE
