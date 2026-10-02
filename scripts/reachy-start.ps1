param(
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'

function Stop-WithMessage {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host $Message -ForegroundColor Red
    exit 1
}

function Invoke-DockerQuietly {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    # Windows PowerShell 5.1 can turn native stderr into a terminating error when
    # ErrorActionPreference is Stop. Suppress stderr for checks; callers report a
    # fixed safe message instead of echoing potentially sensitive Compose output.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& docker @Arguments 2>$null)
        $exitCode = $LASTEXITCODE
    } catch {
        $output = @()
        $exitCode = 1
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    return [pscustomobject]@{ ExitCode = $exitCode; Output = $output }
}

function Invoke-DockerVisible {
    param(
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    # Preserve normal build/start progress while preventing native stderr from
    # aborting before we can inspect the command's exit code on PowerShell 5.1.
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & docker @Arguments 2>&1 | ForEach-Object { Write-Host $_.ToString() }
        $exitCode = $LASTEXITCODE
    } catch {
        Write-Host $_.Exception.Message
        $exitCode = 1
    } finally {
        $ErrorActionPreference = $previousPreference
    }

    return $exitCode
}

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$composeFile = Join-Path $repoRoot 'docker-compose.yml'
$envFile = Join-Path $repoRoot '.env'

if (-not (Test-Path -LiteralPath $composeFile -PathType Leaf)) {
    Stop-WithMessage "Compose file not found: $composeFile"
}
if (-not (Test-Path -LiteralPath $envFile -PathType Leaf)) {
    Stop-WithMessage "Configuration file not found: $envFile. Create it from .env.example, then set the app's existing configuration. This helper will not create or overwrite .env."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Stop-WithMessage 'Docker CLI was not found. Install/start Docker Desktop, then run this helper again. No build was started.'
}

$dockerInfo = Invoke-DockerQuietly -Arguments @('info', '--format', '{{.ServerVersion}}')
if ($dockerInfo.ExitCode -ne 0) {
    Stop-WithMessage 'Docker Engine is not available. Start Docker Desktop and wait until it is ready, then retry. No build was started.'
}

$composeArgs = @(
    'compose',
    '--project-directory', $repoRoot,
    '--file', $composeFile,
    '--env-file', $envFile,
    '--profile', 'reachy'
)

# Ask Compose to resolve .env interpolation and env_file values. Keep the resulting
# configuration in memory and inspect only the bridge service's environment map.
$resolved = Invoke-DockerQuietly -Arguments ($composeArgs + @('config', '--format', 'json'))
if ($resolved.ExitCode -ne 0 -or $resolved.Output.Count -eq 0) {
    Stop-WithMessage 'Compose could not resolve the repository configuration. Check docker-compose.yml and .env; no configuration values were displayed and no build was started.'
}

try {
    $configuration = ($resolved.Output -join [Environment]::NewLine) | ConvertFrom-Json -ErrorAction Stop
} catch {
    Stop-WithMessage 'Compose returned an unreadable configuration. Check Docker Compose; no configuration values were displayed and no build was started.'
}

$bridgeService = $configuration.services.'reachy-bridge'
if ($null -eq $bridgeService -or $null -eq $bridgeService.environment) {
    Stop-WithMessage 'The resolved Compose file has no reachy-bridge environment map. No build was started.'
}
$bridgeEnvironment = $bridgeService.environment
$deviceToken = ([string]$bridgeEnvironment.REACHY_DEVICE_TOKEN).Trim()
$robotBackend = ([string]$bridgeEnvironment.ROBOT_BACKEND).Trim().ToLowerInvariant()
if ([string]::IsNullOrWhiteSpace($robotBackend)) {
    $robotBackend = 'reachy'
}
if ($robotBackend -notin @('reachy', 'video')) {
    Stop-WithMessage 'ROBOT_BACKEND must be reachy or video. No build was started.'
}
if ([string]::IsNullOrWhiteSpace($deviceToken)) {
    Stop-WithMessage 'REACHY_DEVICE_TOKEN is missing. Pair the bridge in the MedAiCare app and configure its rdv1. token in .env. No build was started.'
}
if (-not $deviceToken.StartsWith('rdv1.', [System.StringComparison]::Ordinal)) {
    Stop-WithMessage 'REACHY_DEVICE_TOKEN is present but invalid. Use the rdv1. device token created by pairing this bridge in the MedAiCare app. No build was started.'
}
if ($robotBackend -eq 'reachy' -and [string]::IsNullOrWhiteSpace([string]$bridgeEnvironment.REACHY_ROBOT_HOST)) {
    Stop-WithMessage 'REACHY_ROBOT_HOST is missing. Use the robot LAN address shown by Reachy Mini Control. No build was started.'
}
if ($robotBackend -eq 'video' -and [string]::IsNullOrWhiteSpace([string]$bridgeEnvironment.VIDEO_PATH)) {
    Stop-WithMessage 'VIDEO_PATH is required when ROBOT_BACKEND=video. No build was started.'
}

if ($CheckOnly) {
    Write-Output 'Reachy bridge configuration is valid. Docker Engine is available; no build or container start was requested.'
    exit 0
}

Write-Output 'Starting the Reachy bridge from the repository Compose file...'
$startExitCode = Invoke-DockerVisible -Arguments ($composeArgs + @('up', '--detach', '--build', 'reachy-bridge'))
if ($startExitCode -ne 0) {
    Stop-WithMessage 'Compose could not start reachy-bridge. Review the Docker output above. No bridge configuration values were displayed by the preflight check.'
}
Write-Output ("Reachy bridge started. Follow its logs with: docker compose --file `"{0}`" --profile reachy logs --follow reachy-bridge" -f $composeFile)
