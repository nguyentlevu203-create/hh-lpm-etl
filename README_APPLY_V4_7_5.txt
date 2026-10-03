HH LPM CEO Daily P&L v4.7.5 overlay

1) Copy this overlay into the repository root, preserving folders.
2) Apply patches/mt_gt_etl_unicode_filename.patch (recommended; fixes NFD/NFC Vietnamese master filenames).
3) Run self-test:
   python scripts/build_ceo_daily_pnl_package_v4_7_5.py --self-test
4) Run package for 2026-09-30:
   powershell -ExecutionPolicy Bypass -File .\run_daily_pnl_v4_7_5.ps1 -ReportDate "2026-09-30"
5) Renderer rule for sheet 01_GT:
   - main GT: reporting_mtd_daily_detail_by_channel_v4_7_5.GT
   - separate block: reporting_mtd_daily_detail_by_channel_v4_7_5.GT_NV001
   - never use inherited v4.7.4 GT block after this upgrade.

Expected source-data check from the user-provided September ledger:
- NV001 / Nguyễn Xuân Mạnh Net Sales = 60,000,000 VND
- Original GT Net Sales = 510,142,602 VND
- Displayed GT excluding NV001 = 450,142,602 VND
- Company sales are unchanged by the split.
