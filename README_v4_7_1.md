# CEO Daily P&L v4.7.1 — Top20 SKU + Inventory Sales Mapping

v4.7.1 is an incremental reporting upgrade on top of v4.7. It does not change the validated P&L, target, cancellation, COGS, or inventory ETL rules.

## Business requirements implemented

1. Every channel SKU block uses **Top 20 best-selling products by MTD sold quantity**. Gift quantity is displayed separately but does not affect ranking.
2. Inventory for a product with multiple lots/expiry dates is shown as **one SKU row with total current stock summed across all lots/expiry dates**.
3. Inventory is attached to the channel Top20 rows only for report display. The package keeps full SKU/inventory data for audit.
4. Top20 rows show `sold_qty_mtd`, `current_stock_qty` and `sold_qty_mtd_to_current_stock_pct` so the report can compare sold quantity with current stock. This ratio is **not sell-through** because opening inventory is not available.
5. `06_Ton_kho_Can_date` should use `inventory_sheet_sku_summary`, one row per inventory SKU/product, and show sold quantity MTD from **NHANH, SHOPEE, TIKTOK**.
6. Inventory SKU mapping uses product_id first, then exact normalized SKU/EAN aliases from `dim.product_master`, `dim.product_identifier_map`, and inventory lot source identifiers. Composite marketplace SKUs are never silently assigned to one stock SKU.
7. `inventory.lot_detail` remains available for audit/drill-down, but it is not the primary Excel table because one SKU may have multiple expiry rows.

## New package blocks

- `top_20_sku_by_channel`
- `top_20_sku_rows`
- `inventory_sheet_sku_summary`
- `inventory_sales_mapping_audit`
- `inventory.sku_summary_for_excel`

### Top20 rank rule

`rank = sold_qty_mtd DESC`, gifts excluded from rank. Only rows with `sold_qty_mtd > 0` are eligible. Maximum 20 rows/channel.

### Inventory stock rule

`current_stock_qty` is the actual resolved snapshot total for the SKU. `stock_qty_sum_all_expiry_dates` is the sum of `expiry_breakdown[].stock_qty` and must equal `current_stock_qty`.

### Inventory sheet sales columns

- `sold_qty_mtd_nhanh`
- `sold_qty_mtd_shopee`
- `sold_qty_mtd_tiktok`
- `sold_qty_mtd_3_channels`
- corresponding sales SKU alias lists for each channel
- `current_stock_qty`
- `sold_qty_mtd_3ch_to_current_stock_pct`

## Install

Extract the overlay into the repository root:

```powershell
Expand-Archive `
  "$env:USERPROFILE\Downloads\ceo_daily_pnl_v4_7_1_repo_overlay.zip" `
  -DestinationPath . `
  -Force
```

Check installation:

```powershell
.\install_ceo_daily_pnl_v4_7_1.ps1
```

Then confirm:

```powershell
.\install_ceo_daily_pnl_v4_7_1.ps1 -Apply
```

## Build package only

If inventory for the report date is already imported:

```powershell
.\run_daily_pnl_v4_7_1.ps1 `
  -ReportDate "2026-08-17" `
  -CompareMode "previous_day"
```

Expected package:

`outbox\packages\ceo_daily_pnl_package_v4_7_1_2026-08-17.json`

## Inventory + package in one command

```powershell
.\run_daily_inventory_and_ceo_v4_7_1.ps1 `
  -ReportDate "2026-08-17" `
  -CompareMode "previous_day"
```

## Data-quality behavior

Unresolved/composite sales SKUs remain explicit in `inventory_sales_mapping_audit`. The builder does not create fake stock for them. This may produce a WARNING but does not corrupt mapped inventory values.
