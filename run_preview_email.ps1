$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

# Credentials must already be set in the environment (see .env.example).
# This script no longer hardcodes any DB/SMTP secret.
foreach ($name in @("ETL_DB_PASSWORD", "SMTP_USER", "SMTP_PASSWORD")) {
    if (-not (Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue)) {
        throw "Missing required environment variable `$env:$name. Set it before running run_preview_email.ps1 (see .env.example)."
    }
}
if (-not $env:ETL_DB_NAME) { $env:ETL_DB_NAME = "DuLieu" }
if (-not $env:ETL_DB_USER) { $env:ETL_DB_USER = "postgres" }
if (-not $env:ETL_DB_HOST) { $env:ETL_DB_HOST = "localhost" }
if (-not $env:ETL_DB_PORT) { $env:ETL_DB_PORT = "5433" }
if (-not $env:SMTP_HOST) { $env:SMTP_HOST = "smtp.gmail.com" }
if (-not $env:SMTP_PORT) { $env:SMTP_PORT = "587" }
if (-not $env:SMTP_FROM_EMAIL) { $env:SMTP_FROM_EMAIL = $env:SMTP_USER }
if (-not $env:SMTP_FROM_NAME) { $env:SMTP_FROM_NAME = "Hệ thống báo cáo LPM" }

Remove-Item Env:REPORT_DATE -ErrorAction SilentlyContinue

$env:DRY_RUN="1"

python scripts\send_staff_scorecard_emails.py

start outbox