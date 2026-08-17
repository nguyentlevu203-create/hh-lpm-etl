param(
    [string]$ReportDate = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd"),
    [ValidateSet("previous_day", "previous_week_same_day", "previous_month_same_day")]
    [string]$CompareMode = "previous_day",
    [string]$VatRates = $env:DAILY_PNL_VAT_RATES,
    [switch]$RequireVatRates,
    [switch]$StoreDb,
    [switch]$WithoutGrowthContext
)

$ErrorActionPreference = "Stop"

$repoRoot = $PSScriptRoot
$builder = Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_0.py"
$outDir = Join-Path $repoRoot "outbox\packages"

if (-not (Test-Path $builder)) {
    throw "Khong tim thay builder: $builder"
}

$arguments = @(
    $builder,
    "--date", $ReportDate,
    "--compare-mode", $CompareMode,
    "--out-dir", $outDir
)

if ($VatRates) {
    $arguments += @("--vat-rates", $VatRates)
}
if ($RequireVatRates) {
    $arguments += "--require-vat-rates"
}
if ($StoreDb) {
    $arguments += "--store-db"
}
if ($WithoutGrowthContext) {
    $arguments += "--without-growth-context"
}

Write-Host "Building CEO daily P&L package for $ReportDate..."
& python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Daily P&L package failed with exit code $LASTEXITCODE"
}

Write-Host "Daily P&L package completed."
