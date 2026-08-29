$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pidFile = Join-Path $projectRoot "data\runtime\api.pid"

if (-not (Test-Path -LiteralPath $pidFile)) {
    exit 0
}
$storedId = 0
if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$storedId)) {
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $storedId" -ErrorAction SilentlyContinue
    if ($null -ne $processInfo -and $processInfo.CommandLine -like "*uvicorn backend.app.main:app*") {
        $childProcesses = Get-CimInstance Win32_Process -Filter "ParentProcessId = $storedId" -ErrorAction SilentlyContinue
        foreach ($childProcess in $childProcesses) {
            if ($childProcess.CommandLine -like "*uvicorn backend.app.main:app*") {
                Stop-Process -Id $childProcess.ProcessId -Force
            }
        }
        Stop-Process -Id $storedId -Force
        Write-Output "api stopped."
    }
}
Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
