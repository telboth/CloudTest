param()

$ErrorActionPreference = "Stop"
$cloudRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$runtimeDir = Join-Path $cloudRoot ".runtime"
$pidsDir = Join-Path $runtimeDir "pids"
$authPendingDir = Join-Path $runtimeDir "auth_pending"

if (Test-Path $pidsDir) {
    Get-ChildItem $pidsDir -Filter *.pid -ErrorAction SilentlyContinue | ForEach-Object {
        $pidValue = Get-Content $_.FullName -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pidValue -and $pidValue -match '^\d+$') {
            Stop-Process -Id ([int]$pidValue) -Force -ErrorAction SilentlyContinue
        }
        Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

foreach ($port in @(8010, 8601, 8602, 8603)) {
    $connections = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($procId in $connections) {
        if ($procId) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
}

if (Test-Path $authPendingDir) {
    Get-ChildItem -Path $authPendingDir -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object {
        Remove-Item -Path $_.FullName -Force -ErrorAction SilentlyContinue
    }
}

Write-Host "Cloud_test stoppet."
