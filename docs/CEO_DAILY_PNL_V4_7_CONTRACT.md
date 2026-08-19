# CEO Daily P&L v4.7 Contract

## Objective

Produce one canonical JSON package capable of feeding a CEO Excel report with P&L, monthly KPI progress, all-SKU sold/gift quantities and warehouse/lot/HSD inventory.

## Core P&L

The v4.6.1 financial/status contract is inherited unchanged. v4.7 must not reclassify or recompute the P&L waterfall independently.

Final P&L line: **LỢI NHUẬN**. EBITDA is forbidden.

## Target contract

Source: `ops.monthly_sales_targets`.

Actual: canonical v4.6.1 `mtd_daily_detail_by_channel.net_sales`.

Mapping:
- `DIGITAL -> NHANH`
- `SHOPEE -> SHOPEE`
- `TIKTOK -> TIKTOK`
- `GT -> GT`
- `MT -> MT`
- `ECOM` is parent/audit only.

Company target:

`TOTAL = GT + MT + NHANH + SHOPEE + TIKTOK`

If ECOM exists:

`ECOM target = SHOPEE target + TIKTOK target`

and ECOM must never be added again into TOTAL.

Required top metrics:
- MTD Net Sales
- Monthly Target Net Sales
- Achievement %
- Remaining Target
- Required Daily Net Sales Remaining
- Forecast Month-End Net Sales
- Forecast Achievement %

Optional data only when supplied by DB:
- Target CM2
- Target CM2 Margin
- Target Day Net Sales
- Target MTD plan
- Target Day / MTD CM2

No target may be invented by equal calendar-day allocation.

## Previous-month same-day contract

`previous_month_same_day_full_pnl` must use the same v4.6.1 daily P&L schema as the current date for all five leaf channels. Calendar overflow is clamped (e.g. 31 March -> 28/29 February).

## SKU quantity contract

`sku_daily_fact` contains all source SKU rows from month start through report date.

`total_push_qty = sold_qty + gift_qty`

Rules:
- GT/MT: explicit `quantity_sale`, `quantity_gift`.
- Nhanh: non-cancelled items; `is_gift` from ETL.
- Shopee: non-cancelled exact `Đã hủy`; `is_gift` from ETL.
- TikTok: non-cancelled exact `Canceled`; item price `0=gift`, `<>0=sold`.

Cancelled quantity is kept separately and never counted as pushed quantity.

Marketplace order-level Net Sales is not allocated down to item SKU unless a source-supported allocation exists. It remains null rather than being invented.

## Product mapping contract

Resolve sales identifiers through `dim.product_master` + `dim.product_identifier_map` when unambiguous.

Composite seller SKU (`A+B+C`) is not mapped to a single product. Status: `COMPOSITE_REQUIRES_COMPONENT_ALLOCATION`.

## Inventory contract

Source: `inventory.stock_lot_snapshots`.

Snapshot selection:

`MAX(snapshot_date) WHERE snapshot_date <= report_date`

Never use a future inventory snapshot when rebuilding a historical CEO report.

Lot detail fields include:
- warehouse
- canonical SKU / EAN / raw SKU
- product name
- lot
- expiry date
- days to expiry
- UOM
- stock qty
- source file

Existing `inventory.v_inventory_alerts_ceo_for_package` is passed through when available. v4.7 does not invent expiry/P1/P2/P3 thresholds if the DB view is missing.

## SKU-to-stock contract

`sku_inventory_summary` joins sales SKU facts to the single resolved inventory snapshot. The stock value shown in each sales sheet and in sheet `06_Ton_kho_Can_date` must come from this same package snapshot.

`stock_after_mtd_push` is the actual current snapshot. It is not inferred as opening stock minus sales.

`near_date_pushed_qty` must remain null in v4.7. True near-date push requires movement / lot attribution in v4.8.

## Excel contract

Exact sheet order:
1. `00_Tong_quan`
2. `01_GT`
3. `02_MT`
4. `03_Nhanh`
5. `04_Shopee`
6. `05_TikTok`
7. `06_Ton_kho_Can_date`

Top of every sales sheet always includes:
- DOANH SỐ LŨY KẾ
- CHỈ TIÊU THÁNG
- % THỰC HIỆN
- CÒN THIẾU
- DỰ BÁO CUỐI THÁNG

Literal `N/D` and `N/A` are forbidden in the JSON package.
