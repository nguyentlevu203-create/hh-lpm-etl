param([switch]$Apply)
$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$required = @(
  "scripts\build_ceo_daily_pnl_package_v4_6.py",
  "scripts\build_ceo_daily_pnl_package_v4_6_1.py",
  "scripts\build_ceo_daily_pnl_package_v4_7.py",
  "scripts\build_ceo_daily_pnl_package_v4_7_1.py",
  "scripts\validate_ceo_daily_pnl_package_v4_6.py",
  "scripts\validate_ceo_daily_pnl_package_v4_6_1.py",
  "scripts\validate_ceo_daily_pnl_package_v4_7.py",
  "scripts\validate_ceo_daily_pnl_package_v4_7_1.py"
)
$missing = @()
foreach ($rel in $required) {
  if (-not (Test-Path (Join-Path $repoRoot $rel))) { $missing += $rel }
}
if ($missing.Count -gt 0) {
  throw ("Missing prerequisite files:`n - " + ($missing -join "`n - "))
}

Write-Host "Running v4.7.1 offline self-tests..."
& python (Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_7_1.py") --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.7.1 builder self-test failed" }
& python (Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_7_1.py") --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.7.1 validator self-test failed" }

if (-not $Apply) {
  Write-Host "CHECK ONLY PASSED. No DB/source changes are needed by v4.7.1."
  Write-Host "Run again with -Apply only to confirm installation state."
  exit 0
}
Write-Host "V4.7.1 CODE UPGRADE PASSED."
Write-Host "No ETL tables were modified. Build with .\run_daily_pnl_v4_7_1.ps1"
