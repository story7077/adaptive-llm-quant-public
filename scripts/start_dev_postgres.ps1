[CmdletBinding()]
param(
    [int]$Port = 55432,
    [string]$Database = "trading_phase0",
    [string]$PostgresBin = $env:POSTGRES_BIN
)

$ErrorActionPreference = "Stop"
$repoRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$localRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot ".local"))
$dataDir = [System.IO.Path]::GetFullPath((Join-Path $localRoot "postgres17"))
$logPath = [System.IO.Path]::GetFullPath((Join-Path $localRoot "postgres17.log"))

if (-not $dataDir.StartsWith($repoRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to initialize PostgreSQL outside repository: $dataDir"
}

if ([string]::IsNullOrWhiteSpace($PostgresBin)) {
    $pgCtlCommand = Get-Command "pg_ctl" -ErrorAction SilentlyContinue
    if ($null -eq $pgCtlCommand) {
        throw "PostgreSQL tools are not on PATH. Set POSTGRES_BIN to the bin directory."
    }
    $pgBin = Split-Path -Parent $pgCtlCommand.Source
}
else {
    $pgBin = [System.IO.Path]::GetFullPath($PostgresBin)
}
$initDb = Join-Path $pgBin "initdb.exe"
$pgCtl = Join-Path $pgBin "pg_ctl.exe"
$psql = Join-Path $pgBin "psql.exe"
$createdb = Join-Path $pgBin "createdb.exe"

foreach ($binary in @($initDb, $pgCtl, $psql, $createdb)) {
    if (-not (Test-Path -LiteralPath $binary)) {
        throw "Required PostgreSQL binary not found: $binary"
    }
}

New-Item -ItemType Directory -Path $localRoot -Force | Out-Null
if (-not (Test-Path -LiteralPath (Join-Path $dataDir "PG_VERSION"))) {
    & $initDb --pgdata=$dataDir --username=postgres --encoding=UTF8 --locale=C --auth-local=trust --auth-host=trust
    if ($LASTEXITCODE -ne 0) {
        throw "initdb failed with exit code $LASTEXITCODE"
    }
}

$statusOutput = & $pgCtl status --pgdata=$dataDir 2>&1
if ($LASTEXITCODE -ne 0) {
    & $pgCtl start --pgdata=$dataDir --log=$logPath --options="-p $Port -h 127.0.0.1" --wait
    if ($LASTEXITCODE -ne 0) {
        throw "pg_ctl start failed with exit code $LASTEXITCODE"
    }
}

$existsOutput = & $psql -X -w -h 127.0.0.1 -p $Port -U postgres -d postgres -Atqc "SELECT 1 FROM pg_database WHERE datname='$Database'"
if ($LASTEXITCODE -ne 0) {
    throw "Database existence query failed with exit code $LASTEXITCODE"
}
$exists = ($existsOutput | Out-String).Trim()
if ($exists -ne "1") {
    & $createdb -w -h 127.0.0.1 -p $Port -U postgres $Database
    if ($LASTEXITCODE -ne 0) {
        throw "createdb failed with exit code $LASTEXITCODE"
    }
}

[pscustomobject]@{
    Status = "running"
    Host = "127.0.0.1"
    Port = $Port
    Database = $Database
    DataDirectory = $dataDir
    Url = "postgresql+psycopg://postgres@127.0.0.1:$Port/$Database"
} | ConvertTo-Json
