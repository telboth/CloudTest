param(
    [string]$PythonExe = "python",
    [string]$DatabaseUrl = "",
    [switch]$UseSqliteFallback
)

$ErrorActionPreference = "Stop"
$cloudRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $cloudRoot
Set-Location $projectRoot

$runtimeDir = Join-Path $cloudRoot ".runtime"
$logsDir = Join-Path $runtimeDir "logs"
$pidsDir = Join-Path $runtimeDir "pids"
$authPendingDir = Join-Path $runtimeDir "auth_pending"
$cloudStorageDir = Join-Path $cloudRoot "storage"
$cloudDbPath = Join-Path $cloudRoot "bug_tracker_cloud.db"
New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
New-Item -ItemType Directory -Path $pidsDir -Force | Out-Null
New-Item -ItemType Directory -Path $cloudStorageDir -Force | Out-Null
New-Item -ItemType Directory -Path $authPendingDir -Force | Out-Null
Get-ChildItem -Path $authPendingDir -Filter *.json -ErrorAction SilentlyContinue | ForEach-Object {
    Remove-Item -Path $_.FullName -Force -ErrorAction SilentlyContinue
}

[System.Environment]::SetEnvironmentVariable("STREAMLIT_CLOUD_TEST_MODE", "true", "Process")
[System.Environment]::SetEnvironmentVariable("CLOUD_TEST_ALLOW_LOCAL_LOGIN", "false", "Process")
[System.Environment]::SetEnvironmentVariable("ENABLE_DEVOPS_IN_UI", "false", "Process")
[System.Environment]::SetEnvironmentVariable("AI_PROVIDER", "openai", "Process")
[System.Environment]::SetEnvironmentVariable("EMBEDDING_PROVIDER", "openai", "Process")
[System.Environment]::SetEnvironmentVariable("STORAGE_DIR", $cloudStorageDir, "Process")
[System.Environment]::SetEnvironmentVariable("ATTACHMENT_STORAGE_BACKEND", "filesystem", "Process")

$resolvedDatabaseUrl = ""
if ($UseSqliteFallback) {
    $resolvedDatabaseUrl = "sqlite:///$($cloudDbPath.Replace('\','/'))"
    [System.Environment]::SetEnvironmentVariable("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "true", "Process")
} elseif ($DatabaseUrl -and $DatabaseUrl.Trim()) {
    [System.Environment]::SetEnvironmentVariable("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "false", "Process")
    $resolvedDatabaseUrl = $DatabaseUrl.Trim()
} elseif ($env:CLOUD_TEST_DATABASE_URL -and $env:CLOUD_TEST_DATABASE_URL.Trim()) {
    [System.Environment]::SetEnvironmentVariable("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "false", "Process")
    $resolvedDatabaseUrl = $env:CLOUD_TEST_DATABASE_URL.Trim()
} elseif ($env:DATABASE_URL -and $env:DATABASE_URL.Trim()) {
    [System.Environment]::SetEnvironmentVariable("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "false", "Process")
    $resolvedDatabaseUrl = $env:DATABASE_URL.Trim()
} else {
    [System.Environment]::SetEnvironmentVariable("CLOUD_TEST_ALLOW_SQLITE_FALLBACK", "false", "Process")
    $resolvedDatabaseUrl = "postgresql+psycopg://bugapp:bugapp@localhost:5432/bug_ticket_system"
}
[System.Environment]::SetEnvironmentVariable("DATABASE_URL", $resolvedDatabaseUrl, "Process")
Write-Host "Cloud_test DATABASE_URL: $resolvedDatabaseUrl"
if ($resolvedDatabaseUrl -like "postgresql*") {
    Write-Host "PostgreSQL-modus aktivert (anbefalt for cloud/samtidige brukere)."
} elseif ($resolvedDatabaseUrl -like "sqlite*") {
    Write-Warning "SQLite fallback aktivert. For cloud-ready drift anbefales PostgreSQL."
}

$apps = @(
    @{ Name = "unified"; Port = 8601; Entry = "unified_app.py" }
)

foreach ($port in @(8010, 8601, 8602, 8603)) {
    $connections = @(Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -ExpandProperty OwningProcess -Unique)
    foreach ($procId in $connections) {
        if ($procId) {
            Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
        }
    }
}

Start-Sleep -Seconds 1

foreach ($app in $apps) {
    $stdoutLog = Join-Path $logsDir "$($app.Name).out.log"
    $stderrLog = Join-Path $logsDir "$($app.Name).err.log"
    foreach ($path in @($stdoutLog, $stderrLog)) {
        if (Test-Path $path) {
            Remove-Item -Path $path -Force -ErrorAction SilentlyContinue
        }
    }

    $process = Start-Process -FilePath $PythonExe `
        -ArgumentList @("-m", "streamlit", "run", $app.Entry, "--server.port", "$($app.Port)") `
        -WorkingDirectory $cloudRoot `
        -RedirectStandardOutput $stdoutLog `
        -RedirectStandardError $stderrLog `
        -PassThru

    Set-Content -Path (Join-Path $pidsDir "$($app.Name).pid") -Value $process.Id -Encoding ascii
    Write-Host "Startet cloud-test $($app.Name) pa http://localhost:$($app.Port)"
}

Write-Host ""
Write-Host "Cloud_test startet:"
Write-Host "  Unified:  http://localhost:8601"
Write-Host "  Logger:   $logsDir"
