# CEO Daily Gmail Individual Sender v1

Mục tiêu: gửi **cùng một file Excel CEO Daily** thành **email riêng cho từng người**.
Mỗi email chỉ có đúng 1 địa chỉ ở trường `To`. Không dùng một danh sách To/Cc/Bcc chung.

## File trong bundle

- `scripts/send_ceo_daily_excel_individual.py`
- `config/ceo_daily_email_recipients.csv`
- `run_send_ceo_daily_individual.ps1`

## 1. Copy vào root repo

Giải nén overlay ngay tại thư mục repo ETL.

Cấu trúc sau khi giải nén:

```text
scripts/send_ceo_daily_excel_individual.py
config/ceo_daily_email_recipients.csv
run_send_ceo_daily_individual.ps1
```

## 2. Khai báo từng người nhận

Sửa file:

```text
config/ceo_daily_email_recipients.csv
```

Format:

```csv
name,email,enabled
Nguyen Van A,a@company.com,true
Nguyen Van B,b@company.com,true
```

Quy tắc:
- Mỗi dòng chỉ có **1 email**.
- `enabled=true` mới được gửi.
- Không để trùng email.
- Vì file CEO có đầy đủ P&L/lợi nhuận, chỉ thêm người thực sự được phép nhận.

## 3. Khai báo Gmail SMTP bằng biến môi trường

Trong PowerShell của phiên chạy:

```powershell
$env:GMAIL_SMTP_USER="youraccount@gmail.com"
$env:GMAIL_SMTP_APP_PASSWORD="YOUR_GMAIL_APP_PASSWORD"
$env:GMAIL_FROM_NAME="HOÀNG HÀ DISTRIBUTION"
```

Không ghi App Password trực tiếp vào script hoặc GitHub.

## 4. Validate trước, chưa gửi

Ví dụ với báo cáo 17/08/2026:

```powershell
python .\scripts\send_ceo_daily_excel_individual.py `
  --date "2026-08-17" `
  --xlsx ".\outbox\reports\Báo cáo ngày 17-08-2026.xlsx" `
  --recipients ".\config\ceo_daily_email_recipients.csv" `
  --validate-only
```

Sender sẽ chặn nếu workbook không đúng contract:
- đúng 7 sheet:
  `00_Tong_quan`, `01_GT`, `02_MT`, `03_Nhanh`, `04_Shopee`, `05_TikTok`, `06_Ton_kho_Can_date`
- không còn `99_Data`
- không còn reference `99_Data`
- không có `EBITDA`
- file XLSX không hỏng
- attachment không quá lớn
- danh sách email hợp lệ và không trùng

## 5. Dry-run: tạo email riêng từng người nhưng chưa gửi

```powershell
python .\scripts\send_ceo_daily_excel_individual.py `
  --date "2026-08-17" `
  --xlsx ".\outbox\reports\Báo cáo ngày 17-08-2026.xlsx" `
  --recipients ".\config\ceo_daily_email_recipients.csv" `
  --dry-run
```

Các file `.eml` sẽ nằm tại:

```text
outbox/email_drafts/ceo_daily_individual/2026-08-17/
```

Mở từng `.eml` để kiểm tra:
- người nhận,
- tiêu đề,
- nội dung,
- file Excel đính kèm.

## 6. Khuyến nghị: gửi thử cho đúng 1 người trước

```powershell
python .\scripts\send_ceo_daily_excel_individual.py `
  --date "2026-08-17" `
  --xlsx ".\outbox\reports\Báo cáo ngày 17-08-2026.xlsx" `
  --recipients ".\config\ceo_daily_email_recipients.csv" `
  --only-email "your-test-email@gmail.com"
```

Email đó phải tồn tại và `enabled=true` trong CSV.

## 7. Gửi thật cho toàn bộ người enabled

```powershell
python .\scripts\send_ceo_daily_excel_individual.py `
  --date "2026-08-17" `
  --xlsx ".\outbox\reports\Báo cáo ngày 17-08-2026.xlsx" `
  --recipients ".\config\ceo_daily_email_recipients.csv"
```

Script giữ một kết nối SMTP nhưng tạo **một EmailMessage riêng cho từng người**.
Không người nhận nào nhìn thấy email của người khác.

## 8. Nên truyền package JSON khi production

Nếu package v4.7.2 còn ở máy:

```powershell
python .\scripts\send_ceo_daily_excel_individual.py `
  --date "2026-08-17" `
  --xlsx ".\outbox\reports\Báo cáo ngày 17-08-2026.xlsx" `
  --package ".\outbox\packages\ceo_daily_pnl_package_v4_7_2_2026-08-17.json" `
  --recipients ".\config\ceo_daily_email_recipients.csv" `
  --dry-run
```

Khi có package:
- sender kiểm ngày package khớp ngày báo cáo;
- nếu `data_quality.can_send=false` thì **không gửi**;
- email hiển thị thêm KPI: doanh số lũy kế, chỉ tiêu tháng, % thực hiện, còn thiếu, dự báo cuối tháng;
- trạng thái Data Quality được hiện trong email.

## 9. Chống gửi trùng

Log:

```text
outbox/email_logs/ceo_daily_individual.jsonl
```

Key chống trùng gồm:
- report type,
- ngày báo cáo,
- checksum SHA256 của file Excel,
- email người nhận.

Nếu cùng file/ngày đã gửi thành công cho người đó, script sẽ `SKIP DUPLICATE`.

Chỉ dùng `--force` khi thực sự muốn gửi lại:

```powershell
python .\scripts\send_ceo_daily_excel_individual.py ... --force
```

## 10. PowerShell wrapper

Có thể chạy:

```powershell
.\run_send_ceo_daily_individual.ps1 `
  -ReportDate "2026-08-17" `
  -XlsxPath ".\outbox\reports\Báo cáo ngày 17-08-2026.xlsx" `
  -RecipientsCsv ".\config\ceo_daily_email_recipients.csv" `
  -DryRun
```

Sau khi xem `.eml` đúng, bỏ `-DryRun` để gửi thật.

## Email body

Email được cá nhân hóa:

```text
Kính gửi <Tên người nhận>,

Em gửi Báo cáo CEO Daily Control Tower ngày 17/08/2026.

[5 KPI nếu có package JSON]

File Excel chi tiết: Báo cáo ngày 17-08-2026.xlsx
```

Workbook được attach nguyên bản; sender không mở/save lại workbook nên không làm thay đổi chart/format của file báo cáo.
