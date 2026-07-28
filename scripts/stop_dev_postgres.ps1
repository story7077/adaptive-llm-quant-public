[CmdletBinding()]
param(
    [string]$PostgresBin = $env:POSTGRES_BIN
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$dataDir = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".local\postgres17"))
if ([string]::IsNullOrWhiteSpace($PostgresBin)) {
    $pgCtlCommand = Get-Command "pg_ctl" -ErrorAction SilentlyContinue
    if ($null -eq $pgCtlCommand) {
        throw "PostgreSQL tools are not on PATH. Set POSTGRES_BIN to the bin directory."
    }
    $pgCtl = $pgCtlCommand.Source
}
else {
    $pgCtl = Join-Path ([System.IO.Path]::GetFullPath($PostgresBin)) "pg_ctl.exe"
}

if (-not $dataDir.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to stop PostgreSQL outside repository: $dataDir"
}
if (-not (Test-Path -LiteralPath (Join-Path $dataDir "PG_VERSION"))) {
    Write-Output "No repository-local PostgreSQL cluster exists."
    exit 0
}

& $pgCtl stop --pgdata=$dataDir --mode=fast --wait
if ($LASTEXITCODE -ne 0) {
    throw "pg_ctl stop failed with exit code $LASTEXITCODE"
}
