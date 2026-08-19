# CEO Daily P&L v4.7.1 Contract

## Inherited unchanged

All v4.7 / v4.6.1 P&L, target, marketplace cancellation, Nhanh status, no-EBITDA, and inventory snapshot rules remain unchanged.

## Top20 channel contract

For GT, MT, NHANH, SHOPEE, TIKTOK:
- source: `top_20_sku_by_channel[channel]`
- maximum rows: 20
- eligible: `sold_qty_mtd > 0`
- rank: `sold_qty_mtd DESC`
- gifts do not affect rank
- inventory fields are populated only when the Top20 product maps unambiguously to an inventory SKU
- `sold_qty_mtd_to_current_stock_pct = sold_qty_mtd / current_stock_qty` when stock > 0
- do not label this ratio as sell-through

## Inventory primary table contract

Excel sheet `06_Ton_kho_Can_date` primary table uses `inventory_sheet_sku_summary`.
Grain: one row per inventory SKU/product.

Stock:
- sum all lot/expiry quantities for the product
- `stock_qty_sum_all_expiry_dates == current_stock_qty`
- preserve `expiry_breakdown` for audit
- preserve earliest/latest expiry date

Sales fields added to inventory SKU:
- NHANH sold qty day/MTD
- SHOPEE sold qty day/MTD
- TIKTOK sold qty day/MTD
- MTD total across the three channels
- source sales SKU aliases by channel

## Mapping contract

Priority:
1. exact `product_id`
2. unique exact normalized canonical SKU / EAN / source SKU alias
3. unresolved if no unique candidate

Normalization: Unicode NFC + collapse whitespace + casefold only. No fuzzy matching and no accent stripping.
Composite `A+B+C` seller SKUs require component allocation and are not assigned to a single inventory SKU.

## Lot attribution limit

The package still cannot state exact near-date quantity already pushed because sales are not attributed to inventory lots. That remains a separate movement/order-to-lot feature.
