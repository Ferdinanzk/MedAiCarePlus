# Build script for MedAiCarePlus Docker image with React frontend
# This copies the frontend source into the backend build context, then builds.

param(
    [switch]$SkipFrontendCopy,
    [switch]$NoCache
)

$ErrorActionPreference = "Stop"

$BackendDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$FrontendSource = Join-Path (Join-Path $BackendDir "..") "medaicareplus-web"
$FrontendBuildDir = Join-Path $BackendDir "frontend_source"

Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  MedAiCarePlus Docker Build" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Cyan

# ── Step 1: Copy frontend source into backend build context ──────────────────
if (-not $SkipFrontendCopy) {
    Write-Host "`n[1/3] Copying frontend source..." -ForegroundColor Green
    if (Test-Path $FrontendBuildDir) {
        Remove-Item -Recurse -Force $FrontendBuildDir
    }
    if (-not (Test-Path $FrontendSource)) {
        Write-Error "Frontend source not found at: $FrontendSource"
    }
    Copy-Item -Recurse $FrontendSource $FrontendBuildDir
    Write-Host "      Copied to frontend_source/" -ForegroundColor Gray
} else {
    Write-Host "`n[1/3] Skipping frontend copy (using existing frontend_source/)" -ForegroundColor Yellow
}

# ── Step 2: Build Docker image ───────────────────────────────────────────────
Write-Host "`n[2/3] Building Docker image..." -ForegroundColor Green
$buildArgs = @("compose", "-f", "docker-compose.dev.yml", "build")
if ($NoCache) { $buildArgs += "--no-cache" }

try {
    & docker $buildArgs
} catch {
    Write-Error "Docker build failed. $_"
}

# ── Step 3: Clean up (optional) ─────────────────────────────────────────────
Write-Host "`n[3/3] Cleaning up frontend_source/..." -ForegroundColor Green
Remove-Item -Recurse -Force $FrontendBuildDir

Write-Host "`n═══════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host "  Run: docker compose up -d" -ForegroundColor Cyan
Write-Host "═══════════════════════════════════════════════════" -ForegroundColor Green
