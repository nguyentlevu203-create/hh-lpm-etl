param(
    [switch]$Apply,
    [switch]$RunEtlSelfTests
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$patcher = Join-Path $repoRoot "tools\apply_ceo_daily_pnl_v4_6_upgrade.py"
$builder = Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_6.py"
$validator = Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_6.py"

if (-not (Test-Path $patcher)) { throw "Missing patcher: $patcher" }

Write-Host "1/4 - v4.6 patcher self-test"
& python $patcher --self-test
if ($LASTEXITCODE -ne 0) { throw "Patcher self-test failed" }

Write-Host "2/4 - Repository compatibility check"
& python $patcher --repo-root $repoRoot
if ($LASTEXITCODE -ne 0) { throw "Repository check failed. No source changes were written." }

if (-not $Apply) {
    Write-Host "CHECK ONLY finished. Re-run with -Apply to write ETL changes."
    exit 0
}

Write-Host "3/4 - Applying v4.6 source changes"
& python $patcher --repo-root $repoRoot --apply
if ($LASTEXITCODE -ne 0) { throw "v4.6 patch apply failed" }

Write-Host "4/4 - Syntax + package self-tests"
& python -m py_compile `
    (Join-Path $repoRoot "scripts\nhanh_etl.py") `
    (Join-Path $repoRoot "scripts\shopee_etl.py") `
    (Join-Path $repoRoot "scripts\tiktok_etl.py") `
    $builder $validator
if ($LASTEXITCODE -ne 0) { throw "Python syntax check failed" }

& python $builder --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6 builder self-test failed" }
& python $validator --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6 validator self-test failed" }

Write-Host "V4.6 CODE UPGRADE PASSED."
Write-Host "Next mandatory step for Shopee/TikTok: restore the historical source exports to data folders and re-run those ETLs before building the CEO package."
Write-Host "Nhanh parent reporting in v4.6 is recomputed directly from raw status in DB, so a stale nhanh.orders.is_cancelled flag does not make the report wrong; re-import Nhanh is recommended to clean the stored flag."
