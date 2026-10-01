param(
    [Parameter(Mandatory = $true)][string]$Dump,
    [Parameter(Mandatory = $true)][string]$Gallery
)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$dumpPath = (Resolve-Path -LiteralPath $Dump).Path
$galleryArchive = (Resolve-Path -LiteralPath $Gallery).Path
$galleryRoot = [IO.Path]::GetFullPath((Join-Path $repoRoot 'models\face_recognition\face_gallery'))
$ledgerPath = Join-Path $repoRoot 'ledger\deletion_ledger.jsonl'
$stagingRoot = Join-Path $repoRoot ('.restore-' + [guid]::NewGuid().ToString('N'))
$commandLog = [IO.Path]::GetTempFileName()
$containerDump = '/tmp/medcare-restore-' + [guid]::NewGuid().ToString('N') + '.dump'

function Invoke-Checked {
    param([string]$Program, [string[]]$Arguments)
    # Windows PowerShell can turn successful native stderr into terminating errors.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Program @Arguments > $commandLog 2>&1
        $commandExit = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    if ($commandExit -ne 0) {
        Get-Content -LiteralPath $commandLog -Tail 20 | Write-Host
        throw "$Program failed (exit $commandExit)"
    }
}

function Set-RestoreMarker {
    $sql = "CREATE TABLE IF NOT EXISTS ops_state (key VARCHAR(40) PRIMARY KEY, value TEXT, updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()); INSERT INTO ops_state (key, value) VALUES ('restore_in_progress', '1') ON CONFLICT (key) DO UPDATE SET value='1', updated_at=NOW();"
    Invoke-Checked docker @('compose', 'exec', '-T', 'postgres', 'psql', '-U', 'medai', '-d', 'medcareai2', '-v', 'ON_ERROR_STOP=1', '-c', $sql)
}

function Assert-WorkspacePath {
    param([string]$Target)
    $absolute = [IO.Path]::GetFullPath($Target)
    if (-not $absolute.StartsWith($repoRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing filesystem operation outside workspace: $absolute"
    }
    $current = $absolute
    while ($current -ne $repoRoot) {
        if (Test-Path -LiteralPath $current) {
            $item = Get-Item -LiteralPath $current -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Refusing filesystem operation through a reparse point: $current"
            }
        }
        $current = [IO.Path]::GetDirectoryName($current)
    }
}

Push-Location -LiteralPath $repoRoot
try {
    if (-not (Test-Path -LiteralPath $ledgerPath -PathType Leaf)) {
        throw 'Host deletion ledger is missing; restore cannot safely continue.'
    }
    # Accept only ordinary files/directories with relative paths before extraction.
    Invoke-Checked tar @('-tf', $galleryArchive)
    foreach ($entry in Get-Content -LiteralPath $commandLog) {
        if ([IO.Path]::IsPathRooted($entry) -or $entry -match '(^|[/\\])\.\.([/\\]|$)|:') {
            throw "Unsafe gallery archive entry: $entry"
        }
    }
    Invoke-Checked tar @('-tvf', $galleryArchive)
    foreach ($entry in Get-Content -LiteralPath $commandLog) {
        if ($entry -notmatch '^[-d]') { throw 'Gallery archive contains a link or special file.' }
    }
    Assert-WorkspacePath $stagingRoot
    New-Item -ItemType Directory -Path $stagingRoot | Out-Null
    Invoke-Checked tar @('-xf', $galleryArchive, '-C', $stagingRoot)
    Invoke-Checked docker @('compose', 'up', '-d', 'postgres')
    Invoke-Checked docker @('compose', 'stop', 'app', 'backup')
    Set-RestoreMarker
    Invoke-Checked docker @('compose', 'cp', $dumpPath, "postgres:$containerDump")
    try {
        Invoke-Checked docker @('compose', 'exec', '-T', 'postgres', 'pg_restore',
            '--clean', '--if-exists', '--exit-on-error', '--single-transaction',
            '-U', 'medai', '-d', 'medcareai2', $containerDump)
    }
    finally {
        # pg_restore replaces ops_state; also leave the marker on a failed restore.
        Set-RestoreMarker
    }
    Assert-WorkspacePath $galleryRoot
    if (Test-Path -LiteralPath $galleryRoot) {
        foreach ($item in Get-ChildItem -LiteralPath $galleryRoot -Force -Recurse) {
            Assert-WorkspacePath $item.FullName
        }
        Get-ChildItem -LiteralPath $galleryRoot -Force | ForEach-Object {
            Assert-WorkspacePath $_.FullName
            Remove-Item -LiteralPath $_.FullName -Recurse -Force
        }
    }
    else {
        New-Item -ItemType Directory -Path $galleryRoot -Force | Out-Null
    }
    Get-ChildItem -LiteralPath $stagingRoot -Force | Copy-Item -Destination $galleryRoot -Recurse -Force
    Invoke-Checked docker @('compose', 'run', '--rm', '--no-deps', 'app',
        'python', '-m', 'app.ops.replay_ledger', '/ledger/deletion_ledger.jsonl')
    Invoke-Checked docker @('compose', 'up', '-d', 'app', 'backup')
    Write-Host 'Restore completed; deletion ledger replayed and services started (exit 0).'
}
catch {
    Write-Error "Restore failed. App and backups are not restarted; resolve the error and rerun restore. $($_.Exception.Message)"
    throw
}
finally {
    try {
        Invoke-Checked docker @('compose', 'exec', '-T', 'postgres', 'rm', '-f', $containerDump)
    }
    catch {
        Write-Warning "Could not remove temporary database dump: $containerDump"
    }
    if (Test-Path -LiteralPath $stagingRoot) {
        Assert-WorkspacePath $stagingRoot
        Remove-Item -LiteralPath $stagingRoot -Recurse -Force
    }
    Remove-Item -LiteralPath $commandLog -Force -ErrorAction SilentlyContinue
    Pop-Location
}
