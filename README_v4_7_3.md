# CEO Daily P&L v4.7.3 — HF1

v4.7.3 is a focused completeness upgrade layered on the current GitHub `main` v4.7.2.

## GitHub basis inspected before upgrade

- `scripts/build_ceo_daily_pnl_package_v4_7_2.py` — `714c83df86bf605ee1d0e8fa263cdbbbd996d9c7`
- `scripts/validate_ceo_daily_pnl_package_v4_7_2.py` — `a48b2a57d1c5279b53801f00244b4793f1cb3a77`
- `scripts/build_ceo_daily_pnl_package_v4_7_1.py` — `8ebf4a4aa6f415d340a837cb8de5bf45fcc7e284`
- `scripts/build_ceo_daily_pnl_package_v4_7.py` — `bef819cbcc5d4521b3113e14ed61ceee49729892`
- `scripts/build_ceo_daily_pnl_package_v4_6_1.py` — `9e7ce7c40b12f2aa5cab0d861afa2f4e8a4b8e7e`

## Why this upgrade exists

v4.7.2 already has:

- full current-MTD daily P&L in `mtd_daily_detail_by_channel`;
- full previous-month same-day P&L in `previous_month_same_day_full_pnl`;
- current-vs-previous daily comparison controlled by `CompareMode`;
- all MT/GT current-MTD customers;
- Top20 SKU by MTD sold quantity with inventory mapping;
- expiry-alert-only inventory sheet source.

The missing piece is a **full previous-MTD same-period P&L** with the same waterfall as current MTD.

For report date `2026-08-18`, v4.7.3 compares:

- Current MTD: `2026-08-01 .. 2026-08-18`
- Previous MTD: `2026-07-01 .. 2026-07-18`

The previous period is rebuilt through the exact v4.6.1 P&L/status logic. It is not reconstructed from Net Sales only and it is not taken from `daily_pnl_by_channel_map.<CHANNEL>.previous`.

## New blocks

- `previous_mtd_daily_detail_by_channel`
- `previous_mtd_pnl_by_channel`
- `previous_mtd_summary`
- `previous_mtd_source_data_quality`
- `mtd_pnl_comparison_by_channel`
- `mtd_pnl_comparison_total`

Each comparison uses the full waterfall through `profit / LỢI NHUẬN`.

## Locked non-changes

- No EBITDA.
- Profit is still the last P&L line.
- CM2 is not renamed to Profit.
- Shopee/TikTok/Nhanh cancellation semantics are unchanged.
- Target logic is unchanged.
- v4.7.2 MT/GT customer logic is unchanged.
- v4.7.2 Top20 SKU/inventory logic is unchanged.
- Inventory HSD alert thresholds are unchanged.

## Install

From repo root:

```powershell
Expand-Archive `
  "$env:USERPROFILE\Downloads\ceo_daily_pnl_v4_7_3_repo_overlay.zip" `
  -DestinationPath . `
  -Force
```

Offline checks:

```powershell
python .\scripts\build_ceo_daily_pnl_package_v4_7_3.py --self-test
python .\scripts\validate_ceo_daily_pnl_package_v4_7_3.py --self-test
```

## Build report package

Example for 18/08/2026:

```powershell
.\run_daily_pnl_v4_7_3.ps1 `
  -ReportDate "2026-08-18" `
  -CompareMode "previous_day"
```

Expected output:

```text
outbox\packages\ceo_daily_pnl_package_v4_7_3_2026-08-18.json
```

`CompareMode` continues to control the **daily** comparison. The previous-MTD comparison is always month-start through the same calendar day of the previous month.

## Excel renderer rule

For monthly P&L comparisons use:

- current MTD: `mtd_pnl_comparison_by_channel.<CHANNEL>.current_mtd`
- previous MTD: `mtd_pnl_comparison_by_channel.<CHANNEL>.previous_mtd`
- lines: `mtd_pnl_comparison_by_channel.<CHANNEL>.pnl_lines`

Do not use `daily_pnl_by_channel_map.<CHANNEL>.previous` for the previous-MTD column.


## HF1 validator compatibility

HF1 removes the exact metadata key named after the forbidden metric from the v4.7.3 add-on contract. Older v4.6 prerequisite validators treated any exact key with that name as if it were a financial metric. The validator now checks actual `pnl_lines` semantically and ignores only the known legacy v4.7 metadata-string false-positive. Financial formulas and data are unchanged.
