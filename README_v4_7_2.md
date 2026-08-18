# CEO Daily P&L v4.7.2

v4.7.2 is a report-data upgrade layered on v4.7.1. It does **not** change the v4.6.1/v4.7 accounting waterfall, cancellation semantics, target logic, or inventory import process.

## Locked changes

### 1. MT / GT all customers MTD

New block: `mtgt_customer_mtd_all`.

It reads `mtgt.sales_lines` from the first day of the report month through `report_date`, excludes non-sales `CVC` lines, and returns **all customers that have current-MTD transactions** for GT and MT. It is not limited to Top 5.

Fields include customer code/name, document count, sold/gift/return quantities, Gross Sales, Net Sales, first/last purchase date, same-period prior-month Net Sales and growth where available.

The block reconciles customer Net Sales to canonical `mtd_daily_detail_by_channel` for GT/MT.

### 2. Top 20 SKU by channel — MTD sold quantity

`top_20_sku_by_channel` is rebuilt with this contract:

- Rank by `sold_qty_mtd` descending.
- Gifts do not affect the ranking.
- Cancelled quantities do not count as sold because upstream `sku_daily_fact` already separates them.
- Inventory mapping priority is **exact SKU first**, exact EAN second, then `product_id` fallback.
- Composite seller SKUs are not collapsed into one stock SKU.
- `current_stock_qty` is the actual resolved inventory snapshot total across **all lots and expiry dates** for that SKU.

Report-facing fields include both:

- `sold_qty_mtd_to_current_stock_pct = sold_qty_mtd / current_stock_qty`
- `current_stock_qty_to_sold_qty_mtd = current_stock_qty / sold_qty_mtd`

Neither metric is labelled sell-through or stock cover.

### 3. Sheet 06 only shows expiry/date alerts

New block: `inventory_expiry_alert_skus`.

The sheet source becomes `inventory_expiry_alert_skus.rows`. Only existing `inventory.v_inventory_alerts_ceo_for_package` rows with non-null `earliest_expiry_date` are included.

v4.7.2 does **not** invent a 30/60/90-day near-expiry threshold. It preserves the existing inventory alert view semantics.

The full inventory SKU summary remains in the package for audit and Top20 stock mapping, but must **not** be rendered as the primary table on `06_Ton_kho_Can_date`.

## Install

```powershell
Expand-Archive `
  "$env:USERPROFILE\Downloads\ceo_daily_pnl_v4_7_2_repo_overlay.zip" `
  -DestinationPath . `
  -Force

.\install_ceo_daily_pnl_v4_7_2.ps1
.\install_ceo_daily_pnl_v4_7_2.ps1 -Apply
```

## Build

```powershell
.\run_daily_pnl_v4_7_2.ps1 `
  -ReportDate "2026-08-17" `
  -CompareMode "previous_day"
```

Expected package:

`outbox\packages\ceo_daily_pnl_package_v4_7_2_2026-08-17.json`

## Excel render contract

- `01_GT` and `02_MT`: use `mtgt_customer_mtd_all.by_channel.GT/MT` for the customer section; do not truncate to Top 5.
- `01_GT` ... `05_TikTok`: use `top_20_sku_by_channel.<CHANNEL>` for Top SKU.
- `06_Ton_kho_Can_date`: use only `inventory_expiry_alert_skus.rows`.
