$ErrorActionPreference = "Stop"

cd C:\Users\ADMIN\Downloads\ETL_production_v3_exact_codes

# =========================
# DB CONFIG
# =========================
$env:ETL_DB_NAME="DuLieu"
$env:ETL_DB_USER="postgres"
$env:ETL_DB_PASSWORD="Vu123"
$env:ETL_DB_HOST="localhost"
$env:ETL_DB_PORT="5433"

# =========================
# GMAIL SMTP CONFIG
# =========================
$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="nguyenthienlevu@gmail.com"
$env:SMTP_PASSWORD="zfjcsqwqatsbscaq"
$env:SMTP_FROM_EMAIL="nguyenthienlevu@gmail.com"
$env:SMTP_FROM_NAME="Hệ thống báo cáo LPM"

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