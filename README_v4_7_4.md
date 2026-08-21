# CEO Daily P&L v4.7.4 — Product Mapping + Gift + Inventory Push

This upgrade closes three linked reporting gaps: Shopee gift classification, mixed short-SKU/EAN identity, and sales/gift-to-current-stock visibility.

## Included
- `config/product_sku_alias_master_v4_7_4.csv`: 102 approved EAN/short-SKU pairs derived from the supplied `MÃ SKU.xlsx`.
- `config/product_sku_alias_review_v4_7_4.csv`: 16 non-standard rows kept out of automatic mapping.
- `scripts/import_product_sku_aliases_v4_7_4.py`: fail-closed DB alias importer; dry-run by default.
- `scripts/shopee_gift_classifier_v4_7_4.py`: deterministic order-item gift classifier.
- `scripts/apply_shopee_gift_patch_v4_7_4.py`: narrow patch for the audited current GitHub `shopee_etl.py` blob.
- `scripts/build_ceo_daily_pnl_package_v4_7_4.py` and validator.
- package blocks for Top20 v4.7.4, gift SKUs, inventory push, mapping audit, and unresolved SKUs.

## Install / self-test
```powershell
Expand-Archive "$env:USERPROFILE\Downloads\ceo_daily_pnl_v4_7_4_repo_overlay.zip" -DestinationPath . -Force
.\install_ceo_daily_pnl_v4_7_4.ps1
```

## 1. Import approved aliases
Always dry-run first:
```powershell
python .\scripts\import_product_sku_aliases_v4_7_4.py --file .\config\product_sku_alias_master_v4_7_4.csv
```
The importer writes nothing unless `--apply` is passed. It refuses to apply when an EAN is unresolved/ambiguous or an existing source SKU points to a different product.

After reviewing the dry-run:
```powershell
python .\scripts\import_product_sku_aliases_v4_7_4.py --file .\config\product_sku_alias_master_v4_7_4.csv --apply
```

## 2. Upgrade Shopee gift classification
Dry-run the source patch:
```powershell
python .\scripts\apply_shopee_gift_patch_v4_7_4.py --file .\scripts\shopee_etl.py --dry-run
```
Then apply:
```powershell
python .\scripts\apply_shopee_gift_patch_v4_7_4.py --file .\scripts\shopee_etl.py
```
The patch expects the audited GitHub blob `7da3c8b9ea9392a18c1d181899c43b893ea837d0` and fails closed on a different source unless explicitly re-audited.

The default promotion-rule CSV is intentionally empty. Only add normal sellable SKUs as gifts when there is source evidence or a confirmed rule. Re-import the relevant Shopee order exports after applying the patch so `is_gift` is rebuilt from source data; do not manually flip flags only.

## 3. Build v4.7.4
```powershell
.\run_daily_pnl_v4_7_4.ps1 -ReportDate "2026-08-18" -CompareMode "previous_day"
```
Top20 product identity is strict by default. Use `-AllowUnmappedTop20` only for diagnostics, not the production CEO report.

## Offline regression test
The builder can enrich an existing v4.7.3 package without DB access:
```powershell
python .\scripts\build_ceo_daily_pnl_package_v4_7_4.py --source-package .\outbox\packages\ceo_daily_pnl_package_v4_7_3_2026-08-18.json --out-dir .\outbox\packages
python .\scripts\validate_ceo_daily_pnl_package_v4_7_4.py .\outbox\packages\ceo_daily_pnl_package_v4_7_4_2026-08-18.json --skip-base
```
`--skip-base` is development-only; production validation must run the full inherited validator chain.

## Renderer source changes
- Top20: `top_20_sku_by_channel_v4_7_4`
- Gift/KM: `gift_sku_mtd_by_channel`
- Channel push/stock: `inventory_push_by_channel`
- Company product push: `company_inventory_push_by_product`
- Mapping audit: `top20_mapping_audit_v4_7_4` and `inventory_push_mapping_audit_v4_7_4`

Keep both EAN and source SKU visible. Do not fabricate current stock for identity-only or unresolved rows.
