$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
$runtimeDir = Join-Path $projectRoot "data\runtime"
$logDir = Join-Path $projectRoot "data\logs"
$pidFile = Join-Path $runtimeDir "api.pid"

New-Item -ItemType Directory -Force -Path $runtimeDir, $logDir | Out-Null

if (Test-Path -LiteralPath $pidFile) {
    $storedId = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$storedId)) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $storedId" -ErrorAction SilentlyContinue
        if ($null -ne $processInfo -and $processInfo.CommandLine -like "*uvicorn backend.app.main:app*") {
            Write-Output "api is already running (PID $storedId)."
            exit 0
        }
    }
}

$processInfo = Start-Process `
    -FilePath $python `
    -ArgumentList "-u", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8000" `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $logDir "api.log") `
    -RedirectStandardError (Join-Path $logDir "api.error.log") `
    -PassThru
Set-Content -LiteralPath $pidFile -Value $processInfo.Id -Encoding ascii
Write-Output "api started (PID $($processInfo.Id))."
