param(
    [string]$ReportDate = (Get-Date).AddDays(-1).ToString("yyyy-MM-dd"),
    [ValidateSet("previous_day", "previous_week_same_day", "previous_month_same_day")]
    [string]$CompareMode = "previous_day",
    [string]$VatRates = $env:DAILY_PNL_VAT_RATES,
    [string]$SkuMap = "config\product_sku_alias_master_v4_7_4.csv",
    [string]$BundleMap = "config\bundle_component_map_v4_7_4.csv",
    [switch]$RequireVatRates,
    [switch]$StoreDb,
    [switch]$WithoutGrowthContext,
    [switch]$AllowMissingTargets,
    [switch]$AllowMissingInventory,
    [switch]$AllowUnmappedTop20
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$builder = Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_7_4.py"
$validator = Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_7_4.py"
$outDir = Join-Path $repoRoot "outbox\packages"

$required = @(
    "scripts\build_ceo_daily_pnl_package_v4_7_3.py",
    "scripts\validate_ceo_daily_pnl_package_v4_7_3.py",
    "scripts\build_ceo_daily_pnl_package_v4_7_2.py",
    "scripts\build_ceo_daily_pnl_package_v4_7_1.py",
    "scripts\build_ceo_daily_pnl_package_v4_7.py",
    "scripts\build_ceo_daily_pnl_package_v4_6_1.py",
    $SkuMap,
    $BundleMap
)
foreach ($rel in $required) {
    $p = Join-Path $repoRoot $rel
    if (-not (Test-Path $p)) { throw "Missing v4.7.4 prerequisite: $p" }
}

if (-not $env:PGHOST -and $env:ETL_DB_HOST) { $env:PGHOST = $env:ETL_DB_HOST }
if (-not $env:PGPORT -and $env:ETL_DB_PORT) { $env:PGPORT = $env:ETL_DB_PORT }
if (-not $env:PGDATABASE -and $env:ETL_DB_NAME) { $env:PGDATABASE = $env:ETL_DB_NAME }
if (-not $env:PGUSER -and $env:ETL_DB_USER) { $env:PGUSER = $env:ETL_DB_USER }
if (-not $env:PGPASSWORD -and $env:ETL_DB_PASSWORD) { $env:PGPASSWORD = $env:ETL_DB_PASSWORD }

$argsBuilder = @(
    $builder,
    "--date", $ReportDate,
    "--compare-mode", $CompareMode,
    "--sku-map", $SkuMap,
    "--bundle-map", $BundleMap,
    "--out-dir", $outDir
)
if ($VatRates) { $argsBuilder += @("--vat-rates", $VatRates) }
if ($RequireVatRates) { $argsBuilder += "--require-vat-rates" }
if ($StoreDb) { $argsBuilder += "--store-db" }
if ($WithoutGrowthContext) { $argsBuilder += "--without-growth-context" }
if ($AllowMissingTargets) { $argsBuilder += "--allow-missing-targets" }
if ($AllowMissingInventory) { $argsBuilder += "--allow-missing-inventory" }
if ($AllowUnmappedTop20) { $argsBuilder += "--allow-unmapped-top20" }

Write-Host "Building CEO Daily P&L v4.7.4 for $ReportDate ..."
& python @argsBuilder
if ($LASTEXITCODE -ne 0) { throw "v4.7.4 builder failed with exit code $LASTEXITCODE" }

$package = Join-Path $outDir ("ceo_daily_pnl_package_v4_7_4_{0}.json" -f $ReportDate)
if (-not (Test-Path $package)) { throw "Builder did not create expected package: $package" }

Write-Host "Validating $package ..."
& python $validator $package --sku-map (Join-Path $repoRoot $SkuMap)
if ($LASTEXITCODE -ne 0) { throw "v4.7.4 validation failed" }

Write-Host "PASS: $package"
Write-Host "v4.7.4: short SKU/EAN identity + bundle push + gift view + stock mapping contract validated."
