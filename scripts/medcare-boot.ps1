<#
Bring the MedCare Reachy stack up after a boot, and keep it matched to the network (14 Oct 2026).

Why this exists (traced after a laptop reboot): Docker Desktop does not start by itself; the laptop's Wi-Fi IPv4 is
DHCP and changes between networks, so .env's DEVICE_BIND (the host IP Docker maps the robot's device port 8001 to)
goes stale, Docker's port forwarder gives up after about 60 s and the container stays "healthy" with no port 8001 on
the host; the robot keeps the old server address (app_url) and never auto-starts its app; the LINE tunnel is never
restarted. Each stage below is a readiness check with a bounded wait (no blind sleeps), ends with OK or FAILED and
the real reason, and is logged to logs\startup-<date>.log and the console as "[STARTUP] <stage>: ...".

    .\scripts\medcare-boot.ps1              # at logon (scripts\install-autostart.ps1 registers it)
    .\scripts\medcare-boot.ps1 -Reconcile   # every 5 minutes: quiet, acts only on drift (IP changed, app down...)

Windows PowerShell 5.1. Secrets are never printed (only whether a variable is set).
#>
[CmdletBinding()]
param(
    [switch]$Reconcile,
    [switch]$NoRobot,
    [switch]$NoLine,
    [string]$RepoRoot = '',
    [string]$ComposeProject = 'medcareai2'
)
# The default is this script's own repo (the folder above scripts\), so a copy in any folder runs against itself.
if (-not $RepoRoot) { $RepoRoot = Split-Path $PSScriptRoot -Parent }

$ErrorActionPreference = 'Continue'
$script:Mode = if ($Reconcile) { 'RECONCILE' } else { 'STARTUP' }
$script:LogDir = Join-Path $RepoRoot 'logs'
$script:LogFile = Join-Path $script:LogDir ("startup-{0}.log" -f (Get-Date -Format 'yyyyMMdd'))
$script:ComposeFile = Join-Path $RepoRoot 'docker-compose.yml'
$script:EnvFile = Join-Path $RepoRoot '.env'
$script:Results = [ordered]@{}   # stage -> 'OK' | 'FAILED' | 'SKIPPED'
$script:Changed = @()            # what this run changed (reconcile mode logs only when this is not empty or a stage failed)
$script:Buffer = New-Object System.Collections.ArrayList
$script:AppContainer = "$ComposeProject-app-1"
$script:TunnelContainer = "$ComposeProject-line-tunnel-1"

# ── logging ───────────────────────────────────────────────────────────────────────────────────────────────────────

function Write-Line([string]$Stage, [string]$Message) {
    $line = '{0} [{1}] {2}: {3}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $script:Mode, $Stage, $Message
    Write-Host $line
    if ($Reconcile) { [void]$script:Buffer.Add($line) } else { Add-Content -LiteralPath $script:LogFile -Value $line -Encoding UTF8 }
}

function Flush-Buffer {
    # Reconcile mode keeps the log quiet: its lines reach the file only when something changed or failed.
    if ($script:Buffer.Count -gt 0) {
        Add-Content -LiteralPath $script:LogFile -Value @($script:Buffer) -Encoding UTF8
        $script:Buffer.Clear()
    }
}

function Set-Stage([string]$Stage, [bool]$Ok, [string]$Message) {
    $script:Results[$Stage] = if ($Ok) { 'OK' } else { 'FAILED' }
    Write-Line $Stage ($(if ($Ok) { 'OK ' } else { 'FAILED: ' }) + $Message)
}

function Skip-Stage([string]$Stage, [string]$Why) {
    $script:Results[$Stage] = 'SKIPPED'
    Write-Line $Stage "skipped: $Why"
}

function Wait-Until([scriptblock]$Check, [int]$TimeoutSeconds, [int]$IntervalSeconds = 3) {
    # Poll a readiness condition until it holds or the deadline passes: the only waiting this script does.
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ($true) {
        $result = $null
        try { $result = & $Check } catch { $result = $null }
        if ($result) { return $result }
        if ((Get-Date) -ge $deadline) { return $null }
        Start-Sleep -Seconds $IntervalSeconds
    }
}

# ── helpers ───────────────────────────────────────────────────────────────────────────────────────────────────────

function Read-EnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $script:EnvFile)) { return $null }
    foreach ($line in Get-Content -LiteralPath $script:EnvFile) {
        if ($line -match ('^\s*' + [regex]::Escape($Name) + '\s*=\s*(.*?)\s*$')) { return $Matches[1].Trim('"').Trim("'") }
    }
    return $null
}

function Set-EnvValue([string]$Name, [string]$Value) {
    # Rewrites one line in place (everything else byte-identical, the file's newline style kept), after a backup.
    $raw = [IO.File]::ReadAllText($script:EnvFile)
    $newline = if ($raw -match "`r`n") { "`r`n" } else { "`n" }
    $pattern = '(?m)^(\s*' + [regex]::Escape($Name) + '\s*=)[^\r\n]*'
    if ($raw -match $pattern) {
        $updated = [regex]::Replace($raw, $pattern, ('${1}' + $Value), 1)
    } else {
        $updated = $raw.TrimEnd("`r", "`n") + $newline + "$Name=$Value" + $newline
    }
    $backup = $script:EnvFile + '.bak-' + (Get-Date -Format 'yyyyMMdd-HHmmss')
    Copy-Item -LiteralPath $script:EnvFile -Destination $backup
    [IO.File]::WriteAllText($script:EnvFile, $updated, (New-Object System.Text.UTF8Encoding($false)))
}

function ConvertTo-BitString([string]$Ip) {
    # An IPv4 address as 32 '0'/'1' characters; $null for anything else (a host name, IPv6).
    $addr = $null
    if (-not [System.Net.IPAddress]::TryParse($Ip, [ref]$addr) -or "$($addr.AddressFamily)" -ne 'InterNetwork') { return $null }
    return -join ($addr.GetAddressBytes() | ForEach-Object { [Convert]::ToString($_, 2).PadLeft(8, '0') })
}

function Test-SameSubnet([string]$A, [string]$B, [int]$Prefix) {
    # True when two IPv4 addresses share their first $Prefix bits (the same /$Prefix network).
    $bitsA = ConvertTo-BitString $A
    $bitsB = ConvertTo-BitString $B
    if (-not $bitsA -or -not $bitsB -or $Prefix -lt 1 -or $Prefix -gt 32) { return $false }
    return $bitsA.Substring(0, $Prefix) -eq $bitsB.Substring(0, $Prefix)
}

function Select-LanCandidate($Candidates, [string]$RobotHost) {
    # The lowest-metric candidate, unless another one's subnet holds the robot's address (REACHY_ROBOT_HOST, an IP).
    $list = @($Candidates | Where-Object { $_ })
    if ($list.Count -eq 0) { return $null }
    if ($RobotHost) {
        foreach ($c in $list) { if (Test-SameSubnet $c.Ip $RobotHost $c.Prefix) { return $c } }
    }
    return $list[0]
}

function Get-LanAdapter {
    # The IPv4 address behind the default route, on a real adapter (not WSL's vEthernet, a VPN TAP or loopback) that is
    # Up and holds a Preferred address: a disconnected Ethernet keeps its old DHCP lease as 'Deprecated', and its route
    # has a lower metric than the Wi-Fi's.
    $routes = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric, InterfaceMetric
    $candidates = @()
    foreach ($route in $routes) {
        $adapter = Get-NetAdapter -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue
        if ($null -eq $adapter) { continue }
        if ("$($adapter.Status)" -ne 'Up') { continue }
        if ($adapter.InterfaceDescription -match 'Hyper-V|WSL|TAP-Windows|VPN|Loopback' -or
            $adapter.Name -match 'vEthernet|WSL|TAP|VPN|Loopback') { continue }
        $ip = Get-NetIPAddress -InterfaceIndex $route.InterfaceIndex -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -notlike '169.254.*' -and "$($_.AddressState)" -eq 'Preferred' } |
            Select-Object -First 1
        if ($null -ne $ip) {
            $candidates += [pscustomobject]@{ Ip = $ip.IPAddress; Prefix = $ip.PrefixLength; Alias = $adapter.Name;
                                              Index = $route.InterfaceIndex; Dhcp = ($ip.PrefixOrigin -eq 'Dhcp') }
        }
    }
    return Select-LanCandidate $candidates (Read-EnvValue 'REACHY_ROBOT_HOST')
}

function Get-Ssid {
    $text = (netsh wlan show interfaces 2>$null | Out-String)
    if ($text -match '(?m)^\s*SSID\s*:\s*(.+?)\s*$') { return $Matches[1] }
    return $null
}

function Invoke-Json([string]$Method, [string]$Url, $Body = $null, [int]$TimeoutSec = 6) {
    # A small JSON call; $null when the host does not answer (the caller decides what that means).
    try {
        if ($null -ne $Body) {
            return Invoke-RestMethod -Method $Method -Uri $Url -TimeoutSec $TimeoutSec -ContentType 'application/json' `
                -Body ($Body | ConvertTo-Json -Compress)
        }
        return Invoke-RestMethod -Method $Method -Uri $Url -TimeoutSec $TimeoutSec
    } catch {
        return $null
    }
}

function Test-HttpAnswers([string]$Url) {
    # True when the host answers HTTP at all (a 404 counts: the device API answers "/" with 404); false on no connection.
    try {
        $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5
        return $true
    } catch {
        return ($null -ne $_.Exception.Response)
    }
}

function Compose {
    # docker compose for this project; returns the exit code (its own progress goes to the log, never to the error stream)
    $output = & docker compose -p $ComposeProject -f $script:ComposeFile @args 2>&1 | Out-String
    $code = $LASTEXITCODE
    if ($output.Trim()) { foreach ($l in ($output.Trim() -split "`r?`n")) { Write-Line 'compose' $l.Trim() } }
    return $code
}

function Get-ContainerFacts([string]$Name) {
    $facts = & docker inspect -f '{{.Id}}|{{.State.Status}}|{{.State.Health.Status}}|{{.State.StartedAt}}' $Name 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $facts) { return $null }
    $parts = "$facts".Trim() -split '\|'
    return [pscustomobject]@{ Id = $parts[0]; Status = $parts[1]; Health = $parts[2]; StartedAt = $parts[3] }
}

function Get-TunnelDrift([string]$Facts, [string[]]$StampLines) {
    # Why the LINE webhook may point at a dead address, or '' when it is current. $Facts is the tunnel container's
    # "<State.Status>|<State.StartedAt>"; the stamp is what line-tunnel.ps1 wrote after its last success. The quick
    # tunnel gets a new address whenever its container starts, so the webhook holds only for the stamped start.
    $parts = "$Facts".Trim() -split '\|'
    if ($parts.Count -lt 2 -or -not $parts[1]) { return 'the tunnel container is not there' }
    if ($parts[0] -ne 'running') { return "the tunnel container is $($parts[0])" }
    $stamped = @($StampLines | Where-Object { $_ -like 'started_at=*' } | ForEach-Object { $_.Substring(11).Trim() }) | Select-Object -First 1
    if (-not $stamped) { return 'no stamp from line-tunnel.ps1 (logs\line-tunnel.stamp): the webhook was not registered for this tunnel start' }
    if ($stamped -ne $parts[1]) { return "the tunnel restarted at $($parts[1]) (the webhook was registered for its start at $stamped)" }
    return ''
}

function Get-RobotServerProblem($Status) {
    # Why the robot app has no working server, from its /api/status: '' when it works with the one in its app_url, and
    # $null while that cannot be told yet. Only a problem re-points app_url: a robot another computer uses is left
    # alone, or the two would keep taking it from each other. No answer yet is not a problem: the server block is
    # null while no bridge runs (the app just started, the bridge restarting after an app_url change), and reachable
    # is false until the first heartbeat is answered (server.state null; the field is robot app 0.17.0's). One failed
    # heartbeat (the other server's 14 s rebuild) is not one either: a server counts as gone only after
    # unreachable_for_seconds reaches 30, the robot's own SERVER_LOST_SECONDS (the field is in every version).
    if ($null -eq $Status -or $null -eq $Status.server) { return $null }
    $server = $Status.server
    if ("$($server.state)" -eq 'not_authorised') { return "its server $($server.url) does not accept it (not_authorised)" }
    if ($server.reachable -eq $true) { return '' }
    $lost = $server.unreachable_for_seconds
    if ($null -ne $lost -and [double]$lost -ge 30) {
        return "its server $($server.url) has not answered for $([math]::Round([double]$lost)) s"
    }
    return $null
}

function Find-Robot([int]$TimeoutSeconds) {
    # The robot's IPv4: .env's REACHY_ROBOT_HOST, else mDNS (reachy-mini.local), else the ARP entry of its MAC
    # (campus networks drop mDNS; the ARP table has the robot once anything spoke to it).
    $mac = (Read-EnvValue 'REACHY_ROBOT_MAC')
    if (-not $mac) { $mac = '88-a2-9e-e6-20-1f' }
    $mac = $mac.ToLower().Replace(':', '-')
    $hint = Read-EnvValue 'REACHY_ROBOT_HOST'
    return Wait-Until {
        if ($hint -and (Test-HttpAnswers "http://$hint`:8000/api/daemon/status")) { return $hint }
        $dns = Resolve-DnsName -Name 'reachy-mini.local' -Type A -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress } | Select-Object -First 1
        if ($dns -and (Test-HttpAnswers "http://$($dns.IPAddress):8000/api/daemon/status")) { return $dns.IPAddress }
        foreach ($line in (arp -a 2>$null)) {
            if ($line -match '(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})' -and $Matches[2].ToLower() -eq $mac) {
                if (Test-HttpAnswers "http://$($Matches[1]):8000/api/daemon/status") { return $Matches[1] }
            }
        }
        return $null
    } $TimeoutSeconds 5
}

# ── the lock: two runs (logon + reconcile) never overlap ──────────────────────────────────────────────────────────

New-Item -ItemType Directory -Force -Path $script:LogDir | Out-Null
# A named mutex, not a PID file: Windows frees it when its process dies, so a power-off cannot leave a lock behind.
# An abandoned mutex (its last owner died holding it) is ours: WaitOne hands it over and reports that by throwing.
$script:Mutex = New-Object System.Threading.Mutex($false, 'Local\MedCareReachyBoot')
$owned = $false
try { $owned = $script:Mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) {
    Write-Host "[$script:Mode] another run is still going; leaving it to finish"
    $script:Mutex.Dispose()
    exit 0
}
Get-ChildItem -LiteralPath $script:LogDir -Filter 'startup-*.log' | Sort-Object Name -Descending |
    Select-Object -Skip 14 | Remove-Item -Force -ErrorAction SilentlyContinue

$exitCode = 0
try {
    Write-Line 'run' ("begins (repo $RepoRoot, project $ComposeProject, user $env:USERNAME)")

    # 1. network: the stack binds the device port to this address, so nothing is right before it exists
    $lan = Wait-Until { Get-LanAdapter } 180 3
    if ($null -eq $lan) {
        Set-Stage 'network' $false 'no IPv4 address with a default route after 180 s (Wi-Fi not connected?)'
    } else {
        $ssid = Get-Ssid
        $profile = Get-NetConnectionProfile -InterfaceIndex $lan.Index -ErrorAction SilentlyContinue
        $category = if ($profile) { "$($profile.NetworkCategory)" } else { 'unknown' }
        Set-Stage 'network' $true ("{0} {1}/{2} ({3}, {4}, network '{5}', profile {6})" -f $lan.Alias, $lan.Ip, $lan.Prefix,
            $(if ($lan.Dhcp) { 'DHCP' } else { 'static' }), 'default route', $(if ($ssid) { $ssid } else { 'wired' }), $category)
        if ($category -eq 'Private') {
            Write-Line 'network' "warning: the network is Private, but the firewall's inbound rules for the Docker backend are Public-only; run scripts\install-autostart.ps1 -Firewall in an admin PowerShell (or set the network to Public) or the robot cannot reach port 8001"
        }
        $subnet = ($lan.Ip -replace '\.\d+$', '.')
        $others = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.InterfaceIndex -ne $lan.Index -and $_.IPAddress -like "$subnet*" }
        foreach ($o in $others) {
            Write-Line 'network' "warning: adapter '$($o.InterfaceAlias)' also holds $($o.IPAddress) in the robot's subnet (Internet Connection Sharing residue); a cable plugged into it would take the robot's traffic"
        }
    }

    # 2. docker: Docker Desktop does not start at boot; start it and wait for the engine
    $engineUp = { & docker info 2>$null | Out-Null; $LASTEXITCODE -eq 0 }
    if (& $engineUp) {
        Set-Stage 'docker' $true 'engine already running'
    } else {
        $exe = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue).'Docker Desktop'
        if (-not $exe -or -not (Test-Path -LiteralPath ($exe.Trim('"')))) {
            $exe = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
        }
        $exe = $exe.Trim('"')
        if (-not (Test-Path -LiteralPath $exe)) {
            Set-Stage 'docker' $false "Docker Desktop is not running and its executable was not found ($exe)"
        } else {
            if (-not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
                Write-Line 'docker' "engine down: starting $exe"
                Start-Process -FilePath $exe | Out-Null
                $script:Changed += 'docker started'
            } else {
                Write-Line 'docker' 'Docker Desktop is running but the engine does not answer yet; waiting'
            }
            $began = Get-Date
            if (Wait-Until $engineUp 300 3) {
                Set-Stage 'docker' $true ("engine ready after {0:N0} s" -f ((Get-Date) - $began).TotalSeconds)
            } else {
                Set-Stage 'docker' $false 'the engine did not answer "docker info" within 300 s (open Docker Desktop and look at its error)'
            }
        }
    }

    # 3. env: DEVICE_BIND must be this laptop's current address, or the robot's port is never bound on the host
    $envChanged = $false
    if ($null -ne $lan) {
        $current = Read-EnvValue 'DEVICE_BIND'
        if ($current -ne $lan.Ip) {
            Set-EnvValue 'DEVICE_BIND' $lan.Ip
            $envChanged = $true
            $script:Changed += "DEVICE_BIND $current -> $($lan.Ip)"
            Set-Stage 'env' $true ("DEVICE_BIND was {0}, now {1} (.env backed up); the app container will be recreated" -f $(if ($current) { $current } else { 'unset' }), $lan.Ip)
        } else {
            Set-Stage 'env' $true "DEVICE_BIND=$current matches the network"
        }
    } else {
        Skip-Stage 'env' 'no network address to bind'
    }
    $bindIp = Read-EnvValue 'DEVICE_BIND'

    # 4. stack: compose up (a changed .env recreates the app; engine auto-restarts never read .env again)
    if ($script:Results['docker'] -ne 'OK') {
        Skip-Stage 'stack' 'no Docker engine'
    } else {
        $before = Get-ContainerFacts $script:AppContainer
        $services = @('postgres', 'app')
        if ((Get-Content -LiteralPath $script:ComposeFile -Raw) -match '(?m)^\s{2}backup:') { $services += 'backup' }
        $code = Compose 'up' '-d' @services
        if ($code -ne 0) {
            Set-Stage 'stack' $false "docker compose up exited $code (see the compose lines above)"
        } else {
            $healthy = Wait-Until { $f = Get-ContainerFacts $script:AppContainer; if ($f -and $f.Health -eq 'healthy') { $f } } 180 3
            if ($null -eq $healthy) {
                $f = Get-ContainerFacts $script:AppContainer
                Set-Stage 'stack' $false ("the app container is not healthy after 180 s (status {0}, health {1}); docker logs {2}" -f $(if ($f) { $f.Status } else { 'absent' }), $(if ($f) { $f.Health } else { '-' }), $script:AppContainer)
            } else {
                $recreated = ($null -eq $before) -or ($before.Id -ne $healthy.Id)
                if ($recreated) { $script:Changed += 'app container recreated' }
                Set-Stage 'stack' $true ("app healthy, started {0}{1}" -f $healthy.StartedAt, $(if ($recreated) { ' (recreated)' } else { ' (already running)' }))
            }
        }
    }

    # 5. ports: the host must really listen on DEVICE_BIND:8001 (Docker's forwarder retries, then gives up silently)
    if ($script:Results['stack'] -ne 'OK') {
        Skip-Stage 'ports' 'the stack is not up'
    } else {
        $listening = { Get-NetTCPConnection -State Listen -LocalPort 8001 -ErrorAction SilentlyContinue | Where-Object { $_.LocalAddress -eq $bindIp } | Select-Object -First 1 }
        $bound = Wait-Until $listening 90 3
        if ($null -eq $bound) {
            Write-Line 'ports' "host port 8001 is not listening on $bindIp after 90 s: recreating the app container once"
            $null = Compose 'up' '-d' '--force-recreate' 'app'
            $script:Changed += 'app container force-recreated (port 8001 missing)'
            $null = Wait-Until { $f = Get-ContainerFacts $script:AppContainer; if ($f -and $f.Health -eq 'healthy') { $f } } 180 3
            $bound = Wait-Until $listening 60 3
        }
        $health = $null
        try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 5 } catch { $health = $null }
        $deviceAnswers = Test-HttpAnswers "http://$bindIp`:8001/"
        $problems = @()
        if ($null -eq $bound) { $problems += "nothing listens on $bindIp`:8001 (Docker could not bind it: is $bindIp this laptop's address?)" }
        if ($null -eq $health) { $problems += 'http://127.0.0.1:8080/health does not answer' }
        if (-not $deviceAnswers) { $problems += "http://$bindIp`:8001/ does not answer" }
        if ($problems.Count -eq 0) {
            Set-Stage 'ports' $true "8080 health ok, device API answers on $bindIp`:8001"
        } else {
            Set-Stage 'ports' $false ($problems -join '; ')
        }
    }

    # 6. line: the webhook tunnel gets a new address every start and nothing else restarts it
    $lineToken = Read-EnvValue 'LINE_CHANNEL_ACCESS_TOKEN'
    if ($NoLine) {
        Skip-Stage 'line' '-NoLine'
    } elseif (-not $lineToken) {
        Skip-Stage 'line' 'LINE_CHANNEL_ACCESS_TOKEN is empty in .env'
    } elseif ($script:Results['stack'] -ne 'OK') {
        Skip-Stage 'line' 'the stack is not up'
    } else {
        $tunnelFacts = (& docker inspect -f '{{.State.Status}}|{{.State.StartedAt}}' $script:TunnelContainer 2>$null | Out-String).Trim()
        $stampFile = Join-Path $script:LogDir 'line-tunnel.stamp'
        $stampLines = if (Test-Path -LiteralPath $stampFile) { @(Get-Content -LiteralPath $stampFile) } else { @() }
        $drift = Get-TunnelDrift $tunnelFacts $stampLines
        if ($Reconcile -and -not $drift) {
            Set-Stage 'line' $true "tunnel running since $(($tunnelFacts -split '\|')[1]), the webhook registered for that start"
        } else {
            if ($Reconcile) { Write-Line 'line' "$drift" }
            Write-Line 'line' "starting the tunnel and registering the webhook (scripts\line-tunnel.ps1; up to a few minutes)"
            $tunnelScript = Join-Path $RepoRoot 'scripts\line-tunnel.ps1'
            try {
                $out = & $tunnelScript *>&1 | Out-String   # *> : Write-Host lines too (2> misses them)
                foreach ($l in ($out.Trim() -split "`r?`n")) { if ($l.Trim() -and $l -notmatch 'NativeCommandError|CategoryInfo|FullyQualifiedErrorId|^\s*\+') { Write-Line 'line' $l.Trim() } }
                if ($out -match 'LINE webhook is set and reachable' -or ($out -match 'set webhook: 200' -and $out -match "'success': True")) {
                    Set-Stage 'line' $true 'tunnel up, webhook registered'
                    $script:Changed += 'LINE tunnel started'
                } else {
                    Set-Stage 'line' $false 'line-tunnel.ps1 did not report success (see its lines above)'
                }
            } catch {
                Set-Stage 'line' $false "line-tunnel.ps1 failed: $($_.Exception.Message)"
            }
        }
    }

    # the server's own [STARTUP] report (GET /health), read after the LINE stage so the tunnel just started shows
    if ($script:Results['ports'] -eq 'OK') {
        $health = $null
        try { $health = Invoke-RestMethod -Uri 'http://127.0.0.1:8080/health' -TimeoutSec 8 } catch { $health = $null }
        if ($health -and $health.startup) {
            $degraded = @($health.startup.degraded)
            Write-Line 'server' ("[STARTUP] report from the app: ready={0}, degraded=[{1}]" -f $health.startup.ready, ($degraded -join ', '))
            foreach ($name in @('database', 'gpt_live', 'ai_settings', 'line', 'reminders', 'network', 'clock')) {
                $stage = $health.startup.stages.$name
                if ($stage) { Write-Line 'server' ("  {0}: {1} {2}" -f $name, $(if ($stage.ok) { 'OK' } else { 'DEGRADED' }), $stage.detail) }
            }
        }
    }

    # 7. robot: find it, start its app if it is down, point it at this server unless it works with another one, wait
    # for its heartbeat
    if ($NoRobot) {
        Skip-Stage 'robot' '-NoRobot'
    } elseif ($script:Results['ports'] -ne 'OK') {
        Skip-Stage 'robot' 'the server is not reachable on its device port, so the robot could not use it'
    } else {
        $robot = Find-Robot $(if ($Reconcile) { 5 } else { 120 })
        if (-not $robot) {
            Set-Stage 'robot' $false 'the robot was not found (REACHY_ROBOT_HOST, reachy-mini.local, ARP by REACHY_ROBOT_MAC); is it on and on this network?'
        } else {
            Write-Line 'robot' "found at $robot (daemon answers)"
            $appStatus = Invoke-Json 'Get' "http://$robot`:8000/api/apps/current-app-status"
            $state = if ($appStatus -and $appStatus.state) { "$($appStatus.state)" } else { 'none' }
            $appName = if ($appStatus -and $appStatus.info) { "$($appStatus.info.name)" } else { '' }
            if ($state -ne 'running' -or $appName -ne 'medcare_reachy') {
                Write-Line 'robot' "the robot app is $state ($appName): asking the daemon to start medcare_reachy"
                $null = Invoke-Json 'Post' "http://$robot`:8000/api/apps/start-app/medcare_reachy" $null 30
                $script:Changed += 'robot app started'
                $up = Wait-Until { Invoke-Json 'Get' "http://$robot`:8042/api/status" } 120 3
                if ($null -eq $up) { Write-Line 'robot' 'warning: the robot app did not answer on :8042 within 120 s' }
            }
            $config = Invoke-Json 'Get' "http://$robot`:8042/api/config"
            $expected = "http://$bindIp`:8001"
            $urlOk = $false
            $otherServer = $false
            $undecided = ''
            if ($null -eq $config) {
                Write-Line 'robot' 'warning: the robot app settings (:8042/api/config) do not answer'
            } elseif ("$($config.app_url)".TrimEnd('/') -eq $expected) {
                $urlOk = $true
                Write-Line 'robot' "app_url already $expected"
            } else {
                $status = Invoke-Json 'Get' "http://$robot`:8042/api/status"
                $busy = $status -and ($status.task_id -or $status.slot_state)
                $noServer = $null
                if (-not $busy -and -not "$($config.app_url)".Trim()) {
                    $noServer = 'it had no app_url'
                } elseif (-not $busy) {
                    # Judged only once the robot's status says: right after its app or bridge starts there is no answer
                    # yet. 60 s: a first heartbeat to a dead address fails after the robot's 10 s timeout, and 30 s more
                    # must pass before that counts.
                    $script:RobotStatus = $status
                    $settled = Wait-Until {
                        $s = Invoke-Json 'Get' "http://$robot`:8042/api/status"
                        $script:RobotStatus = $s
                        if ($s -and ($s.task_id -or $s.slot_state)) { return [pscustomobject]@{ Busy = $true; Problem = $null } }
                        $p = Get-RobotServerProblem $s
                        if ($null -ne $p) { return [pscustomobject]@{ Busy = $false; Problem = $p } }
                        return $null
                    } 60 3
                    if ($null -eq $settled) {
                        $s = $script:RobotStatus
                        $appState = if ($s) { "$($s.state)" } else { 'no answer' }
                        $serverState = if ($s -and $s.server -and $s.server.state) { "$($s.server.state)" } else { '-' }
                        $lost = if ($s -and $s.server -and $null -ne $s.server.unreachable_for_seconds) { "$($s.server.unreachable_for_seconds) s" } else { '-' }
                        $undecided = "its status did not show within 60 s whether that server works for it (robot app $appState, server state $serverState, unreachable for $lost)"
                    } else {
                        $busy = $settled.Busy
                        $noServer = $settled.Problem
                    }
                }
                if ($busy) {
                    Write-Line 'robot' "app_url is $($config.app_url) but a session is running on the robot: deferred to the next reconcile"
                } elseif ($undecided) {
                    Write-Line 'robot' "app_url is $($config.app_url) and $undecided"
                } elseif (-not $noServer) {
                    $otherServer = $true
                    Write-Line 'robot' "app_url is $($config.app_url) and the robot works with that server (it answers and accepts the robot): left alone"
                } else {
                    $answer = Invoke-Json 'Post' "http://$robot`:8042/api/config" @{ app_url = $expected } 20
                    if ($null -ne $answer) {
                        $urlOk = $true
                        $script:Changed += "robot app_url $($config.app_url) -> $expected"
                        Write-Line 'robot' "app_url set to $expected (was $($config.app_url); $noServer); the robot restarts its bridge"
                    } else {
                        Write-Line 'robot' "warning: setting app_url on the robot failed"
                    }
                }
            }
            if ($otherServer) {
                # no heartbeat comes to this server while the robot uses the other one
                Skip-Stage 'robot' "the robot works with another server ($($config.app_url))"
            } elseif ($undecided) {
                Skip-Stage 'robot' "app_url $($config.app_url) left alone until the next reconcile (whether its server works for the robot is not known yet)"
            } else {
                $heartbeat = Wait-Until {
                    $age = (& docker compose -p $ComposeProject -f $script:ComposeFile exec -T postgres psql -U medai -d medcareai2 -tAc "select coalesce(extract(epoch from (now()-max(last_seen_at)))::int, 99999) from reachy_device where revoked_at is null" 2>$null | Out-String).Trim()
                    if ($age -match '^\d+$' -and [int]$age -lt 30) { [int]$age } else { $null }
                } 90 5
                if ($null -ne $heartbeat) {
                    Set-Stage 'robot' $true "heartbeat received $heartbeat s ago (app_url $(if ($urlOk) { 'matches' } else { 'NOT confirmed' }))"
                } else {
                    $why = if ($urlOk) { 'the robot app is not sending heartbeats yet (check its log: journalctl -u reachy-mini-daemon on the robot)' } else { 'its app_url could not be confirmed' }
                    Set-Stage 'robot' $false "no heartbeat on the server within 90 s; $why"
                }
            }
        }
    }
} catch {
    Write-Line 'run' "unexpected error: $($_.Exception.Message) at $($_.InvocationInfo.PositionMessage)"
    $exitCode = 1
} finally {
    $failed = @($script:Results.GetEnumerator() | Where-Object { $_.Value -eq 'FAILED' } | ForEach-Object { $_.Key })
    $summary = ($script:Results.GetEnumerator() | ForEach-Object { "$($_.Key)=$($_.Value)" }) -join ', '
    if ($failed.Count -gt 0) {
        Write-Line 'summary' "NOT READY: $($failed -join ', ') failed ($summary)"
        if ($failed -contains 'docker' -or $failed -contains 'stack' -or $failed -contains 'ports') { $exitCode = 1 }
    } else {
        Write-Line 'summary' "ready ($summary)$(if ($script:Changed.Count) { '; changed: ' + ($script:Changed -join '; ') })"
    }
    if ($Reconcile -and ($script:Changed.Count -gt 0 -or $failed.Count -gt 0)) { Flush-Buffer }
    if (-not $Reconcile) { Flush-Buffer }
    try { $script:Mutex.ReleaseMutex() } catch { }
    $script:Mutex.Dispose()
}
exit $exitCode
