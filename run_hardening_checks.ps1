param(
    [string]$PythonExe = "python"
)

$ErrorActionPreference = "Stop"
$cloudRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectRoot = Split-Path -Parent $cloudRoot
Set-Location $projectRoot

Write-Host "Kjorer compile-sjekk..."
& $PythonExe -m py_compile `
    "$cloudRoot\unified_app.py" `
    "$cloudRoot\foundation.py" `
    "$cloudRoot\storage_backend.py" `
    "$cloudRoot\parity_smoke_test.py" `
    "$cloudRoot\scripts\db_maintenance.py" `
    "$cloudRoot\scripts\db_verify.py" `
    "$cloudRoot\app\services\migrations.py"

Write-Host "Kjorer DB-verifisering..."
& $PythonExe "$cloudRoot\scripts\db_verify.py" --strict

Write-Host "Kjorer parity smoke-test..."
& $PythonExe "$cloudRoot\parity_smoke_test.py"

Write-Host ""
Write-Host "Hardening checks fullfort uten feil."
