MT/GT v4.13 total payment revenue patch

Changes:
- Main revenue stored in mtgt.sales_lines.gross_revenue and net_revenue is read directly from Excel column "Tổng thanh toán".
- "Tổng thanh toán" is now required for MT/GT sales ledger files.
- Revenue is no longer recalculated from quantity * unit_price and no longer falls back to "Doanh số bán".
- CVC amount uses abs("Tổng thanh toán").

Apply:
Expand-Archive .\v4_13_mtgt_total_payment_revenue_patch.zip -DestinationPath . -Force

Then truncate/import MTGT if you need historical rows recalculated.
