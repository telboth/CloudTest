param(
    [string]$PythonExe = "python",
    [string]$DatabaseUrl = "",
    [switch]$UseSqliteFallback
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $scriptRoot "start_cloud_test.ps1"

if (-not (Test-Path $startScript)) {
    throw "Fant ikke start_cloud_test.ps1 i Cloud_test-mappen."
}

& powershell -NoProfile -ExecutionPolicy Bypass -File $startScript -PythonExe $PythonExe -DatabaseUrl $DatabaseUrl -UseSqliteFallback:$UseSqliteFallback
