# CEO Daily P&L v4.7 — KPI + SKU + Inventory

## 1. Upgrade basis

This upgrade was written after re-reading GitHub `nguyentlevu203-create/hh-lpm-etl` `main` at commit:

`8c977bb9e4b6a52e473ef32edbf4b6d5eb103889`

The inspected GitHub `main` still contains the v4.5 daily P&L builder and the legacy ETL source. The validated v4.6/v4.6.1 upgrades were not committed to `main`, so this bundle is self-contained: it includes the v4.6 and v4.6.1 prerequisite builders/validators/patchers plus the new v4.7 layer.

The installer is state-aware:
- if a local repo is already on the v4.6.1 status contract, it **skips** the old prerequisite patchers;
- if starting from the inspected GitHub v4.5 state, it applies v4.6 then v4.6.1 before enabling v4.7.

## 2. What v4.7 adds

v4.7 keeps the v4.6.1 P&L waterfall unchanged and adds:

- `target_progress`
  - monthly target from `ops.monthly_sales_targets`;
  - actual MTD from canonical v4.6.1 `net_sales`, not the legacy Gmail actual calculation;
  - `% thực hiện = MTD Net Sales / monthly target`;
  - remaining target, required daily run-rate, forecast and forecast achievement;
  - ECOM is parent/audit only; TOTAL is the five leaf channels and never double-counts ECOM;
  - optional Target CM2 and daily target fields when DB schema/data exists.

- `previous_month_same_day_full_pnl`
  - full same-schema P&L for the same day of the previous month;
  - all five leaf channels + total.

- `sku_daily_fact`
  - all SKU rows by day/channel from month start to report date;
  - sold qty, gift qty, total push qty, cancelled qty, sold/gift COGS;
  - GT/MT use explicit `quantity_sale` / `quantity_gift`;
  - Nhanh uses `order_items.is_gift` and v4.6.1 exact normalized cancellation;
  - Shopee uses `order_items.is_gift` and exact normalized `Đã hủy`;
  - TikTok uses item `price=0` gift / `price<>0` sold and exact normalized `Canceled`.

- `inventory`
  - latest `inventory.stock_lot_snapshots.snapshot_date <= report_date`;
  - never uses a future snapshot;
  - warehouse, SKU/EAN, lot, HSD, UOM, stock, days to expiry;
  - existing `inventory.v_inventory_alerts_ceo_for_package` is preserved verbatim when available;
  - v4.7 does not invent P1/P2/P3 or near-date thresholds when the view is unavailable.

- `sku_inventory_summary`
  - per-channel SKU sold/gift/push day + MTD;
  - linked to the same inventory snapshot used by sheet 06;
  - current stock, HN / HCM thường / HCM-NHÃN TEM stock (plus HCM total), earliest HSD and alert metadata.

- `company_sku_inventory_summary`
  - company-level SKU push + stock view.

- `excel_render_contract`
  - exactly seven sheets:
    `00_Tong_quan`, `01_GT`, `02_MT`, `03_Nhanh`, `04_Shopee`, `05_TikTok`, `06_Ton_kho_Can_date`.

## 3. Important contracts

### Top KPI is mandatory
Every sales sheet must show at the top:

`DOANH SỐ LŨY KẾ | CHỈ TIÊU THÁNG | % THỰC HIỆN | CÒN THIẾU | DỰ BÁO CUỐI THÁNG`

Target is never replaced by forecast.

### August 2026 target mapping already supported by the DB design
The observed target rows use:
- `GT -> GT`
- `MT -> MT`
- `DIGITAL -> NHANH`
- `ECOM -> parent only`
- `SHOPEE -> SHOPEE`
- `TIKTOK -> TIKTOK`

v4.7 calculates TOTAL from leaf channels. If ECOM is present, it validates `ECOM = SHOPEE + TIKTOK`.

### No fake N/D
- confirmed no-sales/applicable metric -> `0`;
- genuinely unavailable optional metric -> `null`, renderer hides or uses em dash;
- literal `N/D` / `N/A` strings are forbidden by validator.

### No fake near-date pushed quantity
v4.7 does **not** claim how many units sold/gift came from a near-expiry lot. Current stock by lot and sold/gift by SKU are separate facts. True `order/SKU -> lot` attribution is planned for v4.8.

### Composite marketplace SKU
A seller SKU such as `A+B+C` is flagged `COMPOSITE_REQUIRES_COMPONENT_ALLOCATION`. v4.7 does not silently map the whole combo to one inventory product.

### No EBITDA
The P&L ends at `LỢI NHUẬN` / `profit`.

## 4. Install

Extract `ceo_daily_pnl_v4_7_repo_overlay.zip` into the repository root.

First run CHECK ONLY:

```powershell
.\install_ceo_daily_pnl_v4_7.ps1
```

Then apply:

```powershell
.\install_ceo_daily_pnl_v4_7.ps1 -Apply
```

Expected final output:

```text
V4.7 CODE UPGRADE PASSED.
```

The installer does **not** alter the PostgreSQL target schema automatically.

## 5. Optional Target CM2 / daily target schema

Review first, then optionally run:

```powershell
psql -h localhost -p 5433 -U postgres -d DuLieu `
  -f .\sql\upgrade_ceo_daily_pnl_v4_7_optional_targets.sql
```

This only adds storage. It does not insert target values and does not equal-distribute monthly targets.

## 6. Inventory load

Use the existing inventory pipeline. Do **not** use its old `--build-package` switch for the v4.7 CEO report.

```powershell
python .\scripts\run_daily_inventory_pipeline.py --date "2026-08-14"
```

If rules were already loaded and unchanged:

```powershell
python .\scripts\run_daily_inventory_pipeline.py --date "2026-08-14" --skip-rules
```

## 7. Build and validate v4.7

```powershell
.\run_daily_pnl_v4_7.ps1 `
  -ReportDate "2026-08-14" `
  -CompareMode "previous_day"
```

Output:

```text
outbox\packages\ceo_daily_pnl_package_v4_7_2026-08-14.json
```

The wrapper runs the v4.7 validator automatically.

## 8. If installing directly from the inspected GitHub main

If the installer says it had to apply the **v4.6 prerequisite**, the source cancellation semantics changed from the legacy GitHub ETL. Historical Shopee/TikTok rows must be rebuilt/re-imported and audited exactly as required by v4.6 before the package is considered production-clean. Do **not** manually update `is_cancelled` flags because dependent COGS/fees/BO/packaging/payout may also need recomputation.

If your local repo was already the validated v4.6.1 deployment, the installer skips those old patchers and you do not need another Shopee/TikTok re-import just for v4.7.

## 9. Audit sources

Run the read-only source audit in psql:

```powershell
psql -h localhost -p 5433 -U postgres -d DuLieu `
  -v report_date="2026-08-14" `
  -f .\sql\audit_ceo_daily_pnl_v4_7_sources.sql
```

This checks target rows, ECOM reconciliation, inventory snapshot dates/columns and prints the actual inventory alert view definitions from PostgreSQL.
