param(
    [string]$ReportDate = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd"),
    [ValidateSet("previous_day", "previous_week_same_day", "previous_month_same_day")]
    [string]$CompareMode = "previous_day",
    [switch]$SkipInventoryRules,
    [switch]$StoreDb,
    [switch]$AllowMissingTargets
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$inventoryRunner = Join-Path $repoRoot "scripts\run_daily_inventory_pipeline.py"
$ceoRunner = Join-Path $repoRoot "run_daily_pnl_v4_7.ps1"

if (-not (Test-Path $inventoryRunner)) { throw "Missing inventory runner: $inventoryRunner" }
if (-not (Test-Path $ceoRunner)) { throw "Missing CEO v4.7 runner: $ceoRunner" }

Push-Location $repoRoot
try {
    $invArgs = @($inventoryRunner, "--date", $ReportDate)
    if ($SkipInventoryRules) { $invArgs += "--skip-rules" }

    Write-Host "1/2 - Loading inventory snapshot for $ReportDate"
    & python @invArgs
    if ($LASTEXITCODE -ne 0) { throw "Inventory pipeline failed" }

    Write-Host "2/2 - Building CEO v4.7 package"
    $ceoArgs = @("-ReportDate", $ReportDate, "-CompareMode", $CompareMode)
    if ($StoreDb) { $ceoArgs += "-StoreDb" }
    if ($AllowMissingTargets) { $ceoArgs += "-AllowMissingTargets" }
    & $ceoRunner @ceoArgs
    if ($LASTEXITCODE -ne 0) { throw "CEO v4.7 package failed" }
} finally {
    Pop-Location
}
