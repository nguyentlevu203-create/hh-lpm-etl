# CEO Daily P&L v4.7.4 Contract

## Scope
v4.7.4 is additive on v4.7.3 HF1. It does not change exact cancellation semantics, targets, previous-MTD comparison, or inventory snapshot math.

## Product identity
One real product may have an EAN and multiple source SKUs. Approved exact aliases are resolved to the same product identity. No EAN truncation, fuzzy product-name matching, or implicit prefix/suffix matching is allowed for automatic mapping.

Resolver order: explicit bundle components -> approved exact short SKU to EAN -> exact EAN -> exact inventory identifier -> product_id fallback.

If identity is known from EAN but the positive-stock snapshot has no row, the product is `MAPPED_IDENTITY_NO_STOCK`: stock remains null, never invented as zero.

## Bundles
Explicit syntax such as `A+B` and `A x 2` is parsed into components for total inventory push. Component SOLD/GIFT roles are `UNSPECIFIED` unless an approved bundle map provides the role. Therefore bundle component push is not silently called sold or gift.

## Inventory push
For a normal item: `total_push = sold_qty + gift_qty`.
For an explicit bundle: `component_push = source_total_push * qty_per_bundle`.
Current stock is the actual latest snapshot <= report date. It is not `opening_stock - push`.

## Shopee gifts
The ETL patch classifies gift rows from source evidence and approved promotion rules. A normal sellable SKU is not globally declared a gift. Partial promotion quantities become REVIEW_REQUIRED rather than guessed.

## Excel renderer
Use `top_20_sku_by_channel_v4_7_4` for Top20, `gift_sku_mtd_by_channel` for the gift table, and `inventory_push_by_channel` for sold/gift/push/stock detail. Bundle Top20 rows must show component stock rather than a fabricated single stock number.
