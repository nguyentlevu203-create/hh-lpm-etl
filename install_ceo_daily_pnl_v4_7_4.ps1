$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
Push-Location $repoRoot
try {
    python .\scripts\build_ceo_daily_pnl_package_v4_7_4.py --self-test
    if ($LASTEXITCODE -ne 0) { throw "Builder self-test failed" }
    python .\scripts\validate_ceo_daily_pnl_package_v4_7_4.py --self-test
    if ($LASTEXITCODE -ne 0) { throw "Validator self-test failed" }
    python .\scripts\shopee_gift_classifier_v4_7_4.py
    if ($LASTEXITCODE -ne 0) { throw "Shopee gift classifier self-test failed" }
    python .\scripts\import_product_sku_aliases_v4_7_4.py --self-test
    if ($LASTEXITCODE -ne 0) { throw "Alias master self-test failed" }
    Write-Host "v4.7.4 code self-tests: PASS"
    Write-Host "Next: dry-run Shopee ETL patch and DB alias import per README_v4_7_4.md"
}
finally { Pop-Location }
