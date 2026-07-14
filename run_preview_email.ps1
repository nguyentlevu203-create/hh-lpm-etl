$ErrorActionPreference = "Stop"

cd C:\Users\ADMIN\Downloads\ETL_production_v3_exact_codes

$env:ETL_DB_NAME="DuLieu"
$env:ETL_DB_USER="postgres"
$env:ETL_DB_PASSWORD="Vu123"
$env:ETL_DB_HOST="localhost"
$env:ETL_DB_PORT="5433"

$env:SMTP_HOST="smtp.gmail.com"
$env:SMTP_PORT="587"
$env:SMTP_USER="nguyenthienlevu@gmail.com"
$env:SMTP_PASSWORD="zfjcsqwqatsbscaq"
$env:SMTP_FROM_EMAIL="nguyenthienlevu@gmail.com"
$env:SMTP_FROM_NAME="Hệ thống báo cáo LPM"

Remove-Item Env:REPORT_DATE -ErrorAction SilentlyContinue

$env:DRY_RUN="1"

python scripts\send_staff_scorecard_emails.py

start outbox