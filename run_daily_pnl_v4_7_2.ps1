param(
    [string]$ReportDate = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd"),
    [ValidateSet("previous_day", "previous_week_same_day", "previous_month_same_day")]
    [string]$CompareMode = "previous_day",
    [string]$VatRates = $env:DAILY_PNL_VAT_RATES,
    [switch]$RequireVatRates,
    [switch]$StoreDb,
    [switch]$WithoutGrowthContext,
    [switch]$AllowMissingTargets,
    [switch]$AllowMissingInventory
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$builder = Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_7_2.py"
$validator = Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_7_2.py"
$outDir = Join-Path $repoRoot "outbox\packages"

if (-not (Test-Path $builder)) { throw "Missing builder: $builder" }
if (-not (Test-Path $validator)) { throw "Missing validator: $validator" }

if (-not $env:PGHOST -and $env:ETL_DB_HOST) { $env:PGHOST = $env:ETL_DB_HOST }
if (-not $env:PGPORT -and $env:ETL_DB_PORT) { $env:PGPORT = $env:ETL_DB_PORT }
if (-not $env:PGDATABASE -and $env:ETL_DB_NAME) { $env:PGDATABASE = $env:ETL_DB_NAME }
if (-not $env:PGUSER -and $env:ETL_DB_USER) { $env:PGUSER = $env:ETL_DB_USER }
if (-not $env:PGPASSWORD -and $env:ETL_DB_PASSWORD) { $env:PGPASSWORD = $env:ETL_DB_PASSWORD }

$arguments = @(
    $builder,
    "--date", $ReportDate,
    "--compare-mode", $CompareMode,
    "--out-dir", $outDir
)
if ($VatRates) { $arguments += @("--vat-rates", $VatRates) }
if ($RequireVatRates) { $arguments += "--require-vat-rates" }
if ($StoreDb) { $arguments += "--store-db" }
if ($WithoutGrowthContext) { $arguments += "--without-growth-context" }
if ($AllowMissingTargets) { $arguments += "--allow-missing-targets" }
if ($AllowMissingInventory) { $arguments += "--allow-missing-inventory" }

Write-Host "Building CEO Daily P&L v4.7.2 for $ReportDate ..."
Write-Host "Scope: all MT/GT MTD customers + Top20 MTD SKU with SKU-first stock mapping + expiry-alert-only sheet 06"
& python @arguments
if ($LASTEXITCODE -ne 0) { throw "v4.7.2 builder failed with exit code $LASTEXITCODE" }

$package = Join-Path $outDir ("ceo_daily_pnl_package_v4_7_2_{0}.json" -f $ReportDate)
if (-not (Test-Path $package)) { throw "Builder did not create expected package: $package" }

Write-Host "Validating $package ..."
& python $validator $package
if ($LASTEXITCODE -ne 0) { throw "v4.7.2 validation failed" }

Write-Host "PASS: $package"
Write-Host "MT/GT customer block: ALL current-MTD customers."
Write-Host "Top SKU: Top 20/channel by MTD sold qty; stock mapped SKU-first and summed across all lots/dates."
Write-Host "Sheet 06 source: only existing inventory expiry/date alert rows."
