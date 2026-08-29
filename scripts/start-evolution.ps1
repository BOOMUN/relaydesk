$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$composeFile = Join-Path $projectRoot "infra\evolution\compose.yml"
$envFile = Join-Path $projectRoot "infra\evolution\.env"
$setupScript = Join-Path $projectRoot "scripts\setup-evolution.ps1"

& $setupScript

docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Desktop is not ready. Start Docker Desktop and run this script again."
}

docker compose --env-file $envFile -f $composeFile up -d
if ($LASTEXITCODE -ne 0) {
    throw "Evolution API containers failed to start."
}

Write-Output "Evolution API is starting at http://127.0.0.1:8081"
