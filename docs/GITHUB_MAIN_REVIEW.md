# GitHub main review used for CEO Daily P&L v4.7

Repository: `nguyentlevu203-create/hh-lpm-etl`

Baseline commit reviewed: `8c977bb9e4b6a52e473ef32edbf4b6d5eb103889` (2026-08-17).

## Observed baseline

- `scripts/build_ceo_daily_pnl_package_v4_5.py` is the latest daily P&L builder committed on `main`.
- `scripts/tiktok_etl.py` on the baseline still contains the legacy broad cancel substring classifier. v4.6/v4.6.1 prerequisite patchers in this bundle are therefore still necessary when installing directly from that baseline.
- `scripts/nhanh_etl.py` already stores item-level `quantity`, `price`, `is_gift`, `unit_cogs`, product code and barcode; v4.7 uses those fields for all-SKU sold/gift facts after the v4.6.1 status classifier is present.
- `scripts/shopee_etl.py` stores item-level `quantity`, `is_gift`, seller SKU, barcode and unit COGS; v4.7 uses the exact v4.6 cancellation contract rather than the legacy stored flag as its reporting predicate.
- `scripts/tiktok_etl.py` stores item-level quantity, price, seller SKU, barcode and unit COGS; v4.7 uses price=0 as gift and exact raw `Canceled` as cancellation.
- `scripts/mt_gt_etl.py` stores explicit `quantity_sale`, `quantity_gift`, `quantity_return`, item code/barcode, unit COGS and line net revenue; these are the preferred GT/MT SKU facts.
- `scripts/inventory_daily_filter.py` already loads warehouse/SKU-or-EAN/lot/HSD/UOM/stock into staging, resolves `dim.product_master`, then commits through `inventory.commit_stock_snapshot`.
- `scripts/run_daily_inventory_pipeline.py` still points its optional `--build-package` switch at the old Control Tower builder. v4.7 intentionally runs inventory first, then runs its own CEO package wrapper separately.

## Design consequence

v4.7 is an additive package layer on top of the validated v4.6.1 financial/status contract. It does not replace the P&L waterfall. It adds target progress, prior-month full P&L, all-SKU sold/gift facts, inventory snapshot/lot/HSD data and the seven-sheet Excel render contract.

The bundle is state-aware: when v4.6.1 status markers already exist locally, the installer does not rerun old source patchers.
