$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$runtimeDir = Join-Path $projectRoot "data\runtime"

foreach ($taskName in @("knowledge-worker", "product-price-worker", "knowledge-scheduler", "conversation-scheduler")) {
    $moduleName = switch ($taskName) {
        "knowledge-worker" { "backend.app.knowledge_worker" }
        "product-price-worker" { "backend.app.product_price_worker" }
        "knowledge-scheduler" { "backend.app.knowledge_scheduler" }
        "conversation-scheduler" { "backend.app.conversation_scheduler" }
    }
    $pidFile = Join-Path $runtimeDir "$taskName.pid"
    if (-not (Test-Path -LiteralPath $pidFile)) {
        continue
    }
    $storedId = 0
    if ([int]::TryParse((Get-Content -LiteralPath $pidFile -Raw).Trim(), [ref]$storedId)) {
        $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $storedId" -ErrorAction SilentlyContinue
        if ($null -ne $processInfo -and $processInfo.CommandLine -like "*$moduleName*") {
            $childProcesses = Get-CimInstance Win32_Process -Filter "ParentProcessId = $storedId" -ErrorAction SilentlyContinue
            foreach ($childProcess in $childProcesses) {
                if ($childProcess.CommandLine -like "*$moduleName*") {
                    Stop-Process -Id $childProcess.ProcessId -Force
                }
            }
            Stop-Process -Id $storedId -Force
            Write-Output "$taskName stopped."
        }
    }
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
}
