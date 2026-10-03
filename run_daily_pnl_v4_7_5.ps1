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
$root = $PSScriptRoot
$builder = Join-Path $root "scripts\build_ceo_daily_pnl_package_v4_7_5.py"
$validator = Join-Path $root "scripts\validate_ceo_daily_pnl_package_v4_7_5.py"
$outDir = Join-Path $root "outbox\packages"
$required = @(
    "scripts\build_ceo_daily_pnl_package_v4_7_4.py",
    "scripts\build_ceo_daily_pnl_package_v4_7_3.py",
    $SkuMap,
    $BundleMap
)
foreach ($rel in $required) { $p=Join-Path $root $rel; if (-not (Test-Path $p)) { throw "Missing v4.7.5 prerequisite: $p" } }
if (-not $env:PGHOST -and $env:ETL_DB_HOST) { $env:PGHOST=$env:ETL_DB_HOST }
if (-not $env:PGPORT -and $env:ETL_DB_PORT) { $env:PGPORT=$env:ETL_DB_PORT }
if (-not $env:PGDATABASE -and $env:ETL_DB_NAME) { $env:PGDATABASE=$env:ETL_DB_NAME }
if (-not $env:PGUSER -and $env:ETL_DB_USER) { $env:PGUSER=$env:ETL_DB_USER }
if (-not $env:PGPASSWORD -and $env:ETL_DB_PASSWORD) { $env:PGPASSWORD=$env:ETL_DB_PASSWORD }
$argsBuilder=@($builder,"--date",$ReportDate,"--compare-mode",$CompareMode,"--sku-map",$SkuMap,"--bundle-map",$BundleMap,"--out-dir",$outDir)
if ($VatRates) { $argsBuilder += @("--vat-rates",$VatRates) }
if ($RequireVatRates) { $argsBuilder += "--require-vat-rates" }
if ($StoreDb) { $argsBuilder += "--store-db" }
if ($WithoutGrowthContext) { $argsBuilder += "--without-growth-context" }
if ($AllowMissingTargets) { $argsBuilder += "--allow-missing-targets" }
if ($AllowMissingInventory) { $argsBuilder += "--allow-missing-inventory" }
if ($AllowUnmappedTop20) { $argsBuilder += "--allow-unmapped-top20" }
Write-Host "Building CEO Daily P&L v4.7.5 for $ReportDate ..."
& python @argsBuilder
if ($LASTEXITCODE -ne 0) { throw "v4.7.5 builder failed with exit code $LASTEXITCODE" }
$package=Join-Path $outDir ("ceo_daily_pnl_package_v4_7_5_{0}.json" -f $ReportDate)
& python $validator $package
if ($LASTEXITCODE -ne 0) { throw "v4.7.5 validation failed" }
Write-Host "PASS: $package"
