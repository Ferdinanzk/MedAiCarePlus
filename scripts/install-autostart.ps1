<#
Register (or remove) the scheduled tasks that run scripts\medcare-boot.ps1 for the current user, and optionally the
firewall rules for the app's ports (14 Oct 2026).

    .\scripts\install-autostart.ps1              # two tasks: at logon (30 s delay) and every 5 minutes (-Reconcile)
    .\scripts\install-autostart.ps1 -Firewall    # also inbound allow rules for TCP 8001 and 8080 (admin PowerShell)
    .\scripts\install-autostart.ps1 -Uninstall

No admin rights are needed for the tasks. Docker Desktop is a user-session program, so the laptop must be signed in:
enable "Start Docker Desktop when you sign in" in Docker Desktop's settings too, and consider Windows auto sign-in if
the laptop may restart unattended (Windows Update).
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = 'C:\medcareai\MedAiCarePlus',
    [switch]$Uninstall,
    [switch]$Firewall
)
$ErrorActionPreference = 'Continue'
$script = Join-Path $RepoRoot 'scripts\medcare-boot.ps1'
$tasks = @(
    @{ Name = 'MedCare Reachy startup';   Schedule = @('/SC', 'ONLOGON', '/DELAY', '0000:30'); Args = '' },
    @{ Name = 'MedCare Reachy reconcile'; Schedule = @('/SC', 'MINUTE', '/MO', '5');           Args = ' -Reconcile' }
)
$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)

if ($Uninstall) {
    foreach ($t in $tasks) {
        & schtasks.exe /Delete /TN $t.Name /F 2>$null | Out-Null
        Write-Host ("task '{0}': {1}" -f $t.Name, $(if ($LASTEXITCODE -eq 0) { 'removed' } else { 'not present' }))
    }
    $shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'MedCare Reachy startup.lnk'
    if (Test-Path -LiteralPath $shortcut) { Remove-Item -LiteralPath $shortcut -Force; Write-Host "startup shortcut removed: $shortcut" }
    if ($Firewall) {
        if ($isAdmin) {
            foreach ($rule in 'MedCare device API 8001', 'MedCare web 8080') {
                Remove-NetFirewallRule -DisplayName $rule -ErrorAction SilentlyContinue
                Write-Host "firewall rule '$rule': removed if it existed"
            }
        } else { Write-Host 'firewall rules not removed: run this in an admin PowerShell with -Firewall' }
    }
    exit 0
}

if (-not (Test-Path -LiteralPath $script)) { Write-Host "missing $script"; exit 1 }
$command = ('powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"{1}' -f $script, '')
# The logon entry: a shortcut in the user's Startup folder (no admin needed; schtasks refuses /SC ONLOGON without it).
$startupDir = [Environment]::GetFolderPath('Startup')
$shortcut = Join-Path $startupDir 'MedCare Reachy startup.lnk'
foreach ($t in $tasks) {
    $tr = $command + $t.Args
    $arguments = @('/Create', '/TN', $t.Name, '/TR', $tr, '/F', '/RL', 'LIMITED') + $t.Schedule
    & schtasks.exe @arguments 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host ("task '{0}': registered ({1})" -f $t.Name, ($t.Schedule -join ' '))
    } elseif ($t.Schedule -contains 'ONLOGON') {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($shortcut)
        $link.TargetPath = 'powershell.exe'
        $link.Arguments = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $script)
        $link.WorkingDirectory = $RepoRoot
        $link.Description = 'Brings the MedCare Reachy stack up after sign-in (scripts\medcare-boot.ps1)'
        $link.Save()
        Write-Host ("task '{0}': schtasks needs admin for a logon task; a Startup-folder shortcut runs it at sign-in instead: {1}" -f $t.Name, $shortcut)
    } else {
        Write-Host ("task '{0}': schtasks exited {1}" -f $t.Name, $LASTEXITCODE)
    }
}
foreach ($t in $tasks) {
    $query = & schtasks.exe /Query /TN $t.Name /FO LIST 2>$null | Out-String
    $next = ($query -split "`r?`n" | Where-Object { $_ -match 'Next Run Time|Status' }) -join '; '
    Write-Host ("  {0}: {1}" -f $t.Name, $next.Trim())
}

if ($Firewall) {
    if (-not $isAdmin) {
        Write-Host 'firewall: -Firewall needs an admin PowerShell (right-click PowerShell, Run as administrator); tasks were registered anyway'
    } else {
        foreach ($rule in @(@{ Name = 'MedCare device API 8001'; Port = 8001 }, @{ Name = 'MedCare web 8080'; Port = 8080 })) {
            if (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue) {
                Write-Host "firewall rule '$($rule.Name)': already present"
            } else {
                New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Protocol TCP -LocalPort $rule.Port `
                    -Action Allow -Profile Any | Out-Null
                Write-Host "firewall rule '$($rule.Name)': added (inbound TCP $($rule.Port), all profiles)"
            }
        }
    }
}

Write-Host ''
Write-Host 'Docker Desktop runs only in a signed-in session: enable "Start Docker Desktop when you sign in" in its Settings > General,'
Write-Host 'and, if the laptop may restart unattended (Windows Update), set Windows to sign you in automatically.'
Write-Host "Log of every run: $RepoRoot\logs\startup-<date>.log"
