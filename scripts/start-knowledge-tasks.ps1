$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runtimeDir = Join-Path $projectRoot "data\runtime"
$logDir = Join-Path $projectRoot "data\logs"

New-Item -ItemType Directory -Force -Path $runtimeDir, $logDir | Out-Null

function Test-AgentDeskTaskProcess {
    param(
        [string]$PidFile,
        [string]$ModuleName
    )
    if (-not (Test-Path -LiteralPath $PidFile)) {
        return $false
    }
    $storedId = 0
    if (-not [int]::TryParse((Get-Content -LiteralPath $PidFile -Raw).Trim(), [ref]$storedId)) {
        return $false
    }
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $storedId" -ErrorAction SilentlyContinue
    return $null -ne $processInfo -and $processInfo.CommandLine -like "*$ModuleName*"
}

function Start-AgentDeskTaskProcess {
    param(
        [string]$Name,
        [string]$ModuleName
    )
    $pidFile = Join-Path $runtimeDir "$Name.pid"
    if (Test-AgentDeskTaskProcess -PidFile $pidFile -ModuleName $ModuleName) {
        Write-Output "$Name is already running."
        return
    }
    $stdoutPath = Join-Path $logDir "$Name.log"
    $stderrPath = Join-Path $logDir "$Name.error.log"
    $processInfo = Start-Process `
        -FilePath $python `
        -ArgumentList "-u", "-m", $ModuleName `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $stdoutPath `
        -RedirectStandardError $stderrPath `
        -PassThru
    Set-Content -LiteralPath $pidFile -Value $processInfo.Id -Encoding ascii
    Write-Output "$Name started (PID $($processInfo.Id))."
}

Start-AgentDeskTaskProcess -Name "knowledge-worker" -ModuleName "backend.app.knowledge_worker"
Start-AgentDeskTaskProcess -Name "product-price-worker" -ModuleName "backend.app.product_price_worker"
Start-AgentDeskTaskProcess -Name "knowledge-scheduler" -ModuleName "backend.app.knowledge_scheduler"
Start-AgentDeskTaskProcess -Name "conversation-scheduler" -ModuleName "backend.app.conversation_scheduler"
