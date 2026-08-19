param(
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
$repoRoot = $PSScriptRoot
$checker = Join-Path $repoRoot "tools\check_ceo_daily_pnl_v4_7_repo.py"
$patch46 = Join-Path $repoRoot "tools\apply_ceo_daily_pnl_v4_6_upgrade.py"
$patch461 = Join-Path $repoRoot "tools\apply_ceo_daily_pnl_v4_6_1_upgrade.py"
$builder47 = Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_7.py"
$validator47 = Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_7.py"

foreach ($p in @($checker, $patch46, $patch461, $builder47, $validator47)) {
    if (-not (Test-Path $p)) { throw "Missing v4.7 bundle file: $p" }
}

Write-Host "1/5 - Self-tests"
& python $checker --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.7 repo checker self-test failed" }
& python $patch46 --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6 prerequisite patcher self-test failed" }
& python $patch461 --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6.1 prerequisite patcher self-test failed" }
& python $builder47 --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.7 builder self-test failed" }
& python $validator47 --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.7 validator self-test failed" }

Write-Host "2/5 - Inspecting current repository state"
$stateText = (& python $checker --repo-root $repoRoot | Out-String)
if ($LASTEXITCODE -ne 0) { throw "Repository is not compatible with the v4.7 upgrade" }
$state = $stateText | ConvertFrom-Json
$stateText | Write-Host

if (-not $Apply) {
    Write-Host "CHECK ONLY finished. No source file was changed by the installer."
    Write-Host ("Planned action: {0}" -f $state.action)
    Write-Host "Re-run: .\install_ceo_daily_pnl_v4_7.ps1 -Apply"
    exit 0
}

Write-Host "3/5 - Applying only missing v4.6/v4.6.1 source prerequisites"
if ($state.v461_source_ready) {
    Write-Host "v4.6.1 ETL status semantics already present. Skipping old prerequisite patchers to avoid downgrade/rewrite."
} else {
    if (-not $state.v46_source_ready) {
        Write-Host "Applying v4.6 exact marketplace/Nhanh prerequisite..."
        & python $patch46 --repo-root $repoRoot --apply
        if ($LASTEXITCODE -ne 0) { throw "v4.6 prerequisite apply failed" }
    }
    Write-Host "Applying v4.6.1 Nhanh normalization prerequisite..."
    & python $patch461 --repo-root $repoRoot --apply
    if ($LASTEXITCODE -ne 0) { throw "v4.6.1 prerequisite apply failed" }
}

Write-Host "4/5 - Syntax + all prerequisite/v4.7 self-tests"
$compileFiles = @(
    (Join-Path $repoRoot "scripts\nhanh_etl.py"),
    (Join-Path $repoRoot "scripts\shopee_etl.py"),
    (Join-Path $repoRoot "scripts\tiktok_etl.py"),
    (Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_6.py"),
    (Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_6_1.py"),
    $builder47,
    (Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_6.py"),
    (Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_6_1.py"),
    $validator47
)
& python -m py_compile @compileFiles
if ($LASTEXITCODE -ne 0) { throw "Python syntax check failed" }

& python (Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_6.py") --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6 builder self-test failed" }
& python (Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_6.py") --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6 validator self-test failed" }
& python (Join-Path $repoRoot "scripts\build_ceo_daily_pnl_package_v4_6_1.py") --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6.1 builder self-test failed" }
& python (Join-Path $repoRoot "scripts\validate_ceo_daily_pnl_package_v4_6_1.py") --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.6.1 validator self-test failed" }
& python $builder47 --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.7 builder self-test failed" }
& python $validator47 --self-test
if ($LASTEXITCODE -ne 0) { throw "v4.7 validator self-test failed" }

Write-Host "5/5 - Final readiness check"
$finalText = (& python $checker --repo-root $repoRoot | Out-String)
if ($LASTEXITCODE -ne 0) { throw "Final repository readiness check failed" }
$final = $finalText | ConvertFrom-Json
$finalText | Write-Host
if (-not $final.v47_runtime_ready) { throw "v4.7 runtime is not ready after apply" }

Write-Host "V4.7 CODE UPGRADE PASSED."
Write-Host "No DB schema migration was auto-applied. Optional Target CM2/daily target schema is in sql\upgrade_ceo_daily_pnl_v4_7_optional_targets.sql."
Write-Host "Next: load inventory snapshot, then run .\run_daily_pnl_v4_7.ps1 -ReportDate '2026-08-14' -CompareMode 'previous_day'"
