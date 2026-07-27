$ErrorActionPreference = "Stop"

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$PythonExecutable = Join-Path $RepositoryRoot ".venv\Scripts\python.exe"
$LogDirectory = Join-Path $RepositoryRoot ".local\logs"
$LogFile = Join-Path $LogDirectory "forward-paper.log"

if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw "Python environment is missing: $PythonExecutable"
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
Set-Location -LiteralPath $RepositoryRoot

$PreviousErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $PythonExecutable -m trading.cli paper serve `
    --run-id "paper_20260728_v4" `
    --host "127.0.0.1" `
    --port 8765 `
    --enable-ai *>> $LogFile
$ProcessExitCode = $LASTEXITCODE
$ErrorActionPreference = $PreviousErrorActionPreference

exit $ProcessExitCode
