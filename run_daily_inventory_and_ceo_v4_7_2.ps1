param(
    [string]$ReportDate = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd"),
    [ValidateSet("previous_day", "previous_week_same_day", "previous_month_same_day")]
    [string]$CompareMode = "previous_day",
    [switch]$SkipRules,
    [switch]$StoreDb,
    [switch]$AllowMissingTargets,
    [switch]$AllowMissingInventory
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$inventoryRunner = Join-Path $repoRoot "scripts\run_daily_inventory_pipeline.py"
$ceoRunner = Join-Path $repoRoot "run_daily_pnl_v4_7_2.ps1"

if (-not (Test-Path $inventoryRunner)) { throw "Missing inventory runner: $inventoryRunner" }
if (-not (Test-Path $ceoRunner)) { throw "Missing CEO runner: $ceoRunner" }

Write-Host "1/2 - Loading inventory snapshot $ReportDate"
$invArgs = @($inventoryRunner, "--date", $ReportDate)
if ($SkipRules) { $invArgs += "--skip-rules" }
& python @invArgs
if ($LASTEXITCODE -ne 0) { throw "Inventory pipeline failed" }

Write-Host "2/2 - Building CEO v4.7.2 package"
$ceoArgs = @{
    ReportDate = $ReportDate
    CompareMode = $CompareMode
}
if ($StoreDb) { $ceoArgs["StoreDb"] = $true }
if ($AllowMissingTargets) { $ceoArgs["AllowMissingTargets"] = $true }
if ($AllowMissingInventory) { $ceoArgs["AllowMissingInventory"] = $true }
& $ceoRunner @ceoArgs
