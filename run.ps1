param(
    [string]$HostAddress = "0.0.0.0",
    [int]$Port = 8000
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python virtual environment not found: $python"
}

$frontendDist = Join-Path $projectRoot "frontend\dist\index.html"
if (-not (Test-Path -LiteralPath $frontendDist)) {
    Push-Location (Join-Path $projectRoot "frontend")
    try {
        npm.cmd run build
    }
    finally {
        Pop-Location
    }
}

& (Join-Path $projectRoot "scripts\start-knowledge-tasks.ps1")

& $python -m uvicorn backend.app.main:app --host $HostAddress --port $Port
