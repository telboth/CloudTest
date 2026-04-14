param(
    [string]$PythonExe = "python",
    [string]$DatabaseUrl = "",
    [switch]$UseSqliteFallback,
    [switch]$SkipMigrations,
    [switch]$ReindexDirtyOnStart,
    [switch]$ReindexWithoutEmbeddings,
    [switch]$SkipSchemaVerify,
    [switch]$AllowMigrationFallback,
    [switch]$EnableLegacySchemaBootstrap
)

$ErrorActionPreference = "Stop"
$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $scriptRoot "start_cloud_test.ps1"

if (-not (Test-Path $startScript)) {
    throw "Fant ikke start_cloud_test.ps1 i CloudTest-mappen."
}

if ($DatabaseUrl -and $DatabaseUrl.Trim()) {
    & $startScript `
        -PythonExe $PythonExe `
        -DatabaseUrl $DatabaseUrl.Trim() `
        -UseSqliteFallback:$UseSqliteFallback `
        -SkipMigrations:$SkipMigrations `
        -ReindexDirtyOnStart:$ReindexDirtyOnStart `
        -ReindexWithoutEmbeddings:$ReindexWithoutEmbeddings `
        -SkipSchemaVerify:$SkipSchemaVerify `
        -AllowMigrationFallback:$AllowMigrationFallback `
        -EnableLegacySchemaBootstrap:$EnableLegacySchemaBootstrap
}
else {
    & $startScript `
        -PythonExe $PythonExe `
        -UseSqliteFallback:$UseSqliteFallback `
        -SkipMigrations:$SkipMigrations `
        -ReindexDirtyOnStart:$ReindexDirtyOnStart `
        -ReindexWithoutEmbeddings:$ReindexWithoutEmbeddings `
        -SkipSchemaVerify:$SkipSchemaVerify `
        -AllowMigrationFallback:$AllowMigrationFallback `
        -EnableLegacySchemaBootstrap:$EnableLegacySchemaBootstrap
}

