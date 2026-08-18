param(
    [Parameter(Mandatory=$true)]
    [string]$ReportDate,

    [Parameter(Mandatory=$true)]
    [string]$XlsxPath,

    [Parameter(Mandatory=$false)]
    [string]$RecipientsCsv = ".\config\ceo_daily_email_recipients.csv",

    [Parameter(Mandatory=$false)]
    [string]$PackagePath = "",

    [switch]$DryRun,
    [switch]$ValidateOnly,
    [switch]$Force,

    [Parameter(Mandatory=$false)]
    [string]$OnlyEmail = ""
)

$ErrorActionPreference = "Stop"

$scriptPath = ".\scripts\send_ceo_daily_excel_individual.py"
if (-not (Test-Path $scriptPath)) {
    throw "Missing sender script: $scriptPath"
}
if (-not (Test-Path $XlsxPath)) {
    throw "Missing Excel report: $XlsxPath"
}
if (-not (Test-Path $RecipientsCsv)) {
    throw "Missing recipients CSV: $RecipientsCsv"
}

$pyArgs = @(
    $scriptPath,
    "--date", $ReportDate,
    "--xlsx", $XlsxPath,
    "--recipients", $RecipientsCsv
)

if ($PackagePath) {
    if (-not (Test-Path $PackagePath)) {
        throw "Missing package JSON: $PackagePath"
    }
    $pyArgs += @("--package", $PackagePath)
}

if ($DryRun) {
    $pyArgs += "--dry-run"
}
if ($ValidateOnly) {
    $pyArgs += "--validate-only"
}
if ($Force) {
    $pyArgs += "--force"
}
if ($OnlyEmail) {
    $pyArgs += @("--only-email", $OnlyEmail)
}

& python @pyArgs
exit $LASTEXITCODE
