$ErrorActionPreference = "Stop"

Set-Location $PSScriptRoot

# =========================
# CREDENTIALS
# Must already be set in the environment (e.g. loaded from a local .env
# that is never committed — see .env.example). This script no longer
# hardcodes any DB/SMTP secret.
# =========================
foreach ($name in @("ETL_DB_PASSWORD", "SMTP_USER", "SMTP_PASSWORD")) {
    if (-not (Get-Item -Path "Env:$name" -ErrorAction SilentlyContinue)) {
        throw "Missing required environment variable `$env:$name. Set it before running run_daily_all.ps1 (see .env.example)."
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

# =========================
# REPORT DATE
# Không set REPORT_DATE để script tự lấy ngày hiện tại - 1
# =========================
Remove-Item Env:REPORT_DATE -ErrorAction SilentlyContinue

# =========================
# LOG FILE
# =========================
$logDir = "logs"
if (!(Test-Path $logDir)) {
    New-Item -ItemType Directory -Path $logDir | Out-Null
}

$today = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$logFile = "logs\daily_run_$today.log"

Start-Transcript -Path $logFile -Append

Write-Host "======================================"
Write-Host "START DAILY ETL + GMAIL REPORT"
Write-Host "Time: $(Get-Date)"
Write-Host "======================================"

Write-Host "Checking DB connection..."
python -c "import os, psycopg2; conn=psycopg2.connect(dbname=os.getenv('ETL_DB_NAME'), user=os.getenv('ETL_DB_USER'), password=os.getenv('ETL_DB_PASSWORD'), host=os.getenv('ETL_DB_HOST'), port=os.getenv('ETL_DB_PORT')); print('DB OK'); conn.close()"

Write-Host "Running Nhanh ETL..."
python scripts\nhanh_etl.py

Write-Host "Running Shopee ETL..."
python scripts\shopee_etl.py

Write-Host "Running TikTok ETL..."
python scripts\tiktok_etl.py

Write-Host "Running MT/GT ETL..."
python scripts\mt_gt_etl.py

Write-Host "Sending Gmail reports..."
$env:DRY_RUN="0"
python scripts\send_staff_scorecard_emails.py

Write-Host "======================================"
Write-Host "DONE DAILY ETL + GMAIL REPORT"
Write-Host "Time: $(Get-Date)"
Write-Host "Log file: $logFile"
Write-Host "======================================"

Stop-Transcript