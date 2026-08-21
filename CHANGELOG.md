# Changelog — hh-lpm-etl

Historical release notes, consolidated from the per-version `README_v4_*.md` files that used to sit at
the repo root (see `docs/PRODUCTION_CLEANUP_MANIFEST.md` for why they were merged here and removed).
Dates are approximate (file/commit dates), not announced release dates. This file does not restate every
implementation line — see `docs/CEO_DAILY_PNL_V4_7_*_CONTRACT.md` for the authoritative per-release
contract and `CLAUDE.md` for current business rules.

## v4.7.4 — Product Mapping + Gift + Inventory Push (current)

Source: `README_v4_7_4.md` (kept at repo root — its path is pinned inside `BUNDLE_MANIFEST.json`).

- Adds product mapping (short-SKU/EAN alias identity) via `config/product_sku_alias_master_v4_7_4.csv`
  (102 approved pairs) and `config/product_sku_alias_review_v4_7_4.csv` (16 non-standard rows kept out of
  automatic mapping), imported by `scripts/import_product_sku_aliases_v4_7_4.py` (fail-closed, dry-run by
  default).
- Adds deterministic Shopee gift classification (`scripts/shopee_gift_classifier_v4_7_4.py`), applied via
  `scripts/apply_shopee_gift_patch_v4_7_4.py`. **This patch has still not been applied to
  `scripts/shopee_etl.py`** — see `CLAUDE.md`'s "Current production entry points" warning and
  `REFACTOR_LATER.md`.
- Adds inventory push/stock visibility blocks: `top_20_sku_by_channel_v4_7_4`, `gift_sku_mtd_by_channel`,
  `inventory_push_by_channel`, `company_inventory_push_by_product`, plus mapping-audit blocks.
- Purely additive on top of the v4.6.1 financial/status contract — no change to the P&L waterfall,
  cancellation semantics, or COGS logic.
- **HF2 hotfix** (`README_HF2.txt`): `scripts/rebind_stale_product_aliases_v4_7_4_HF2.py` — resolves stale
  SHOPEE/TIKTOK short-SKU aliases pointing at a pre-EAN-master product, by re-pointing (never
  deleting/merging) alias rows when there is no contradictory EAN evidence.

## v4.7.3 (HF1) — Previous-MTD comparison

Source: `README_v4_7_3.md` (superseded, content merged here, file removed in the 2026-08 cleanup pass).

- Adds a full previous-MTD same-period P&L (`previous_mtd_daily_detail_by_channel`,
  `previous_mtd_pnl_by_channel`, `previous_mtd_summary`, `mtd_pnl_comparison_by_channel/total`),
  rebuilt through the exact v4.6.1 waterfall — not reconstructed from Net Sales only.
- HF1: validator no longer false-flags a legacy v4.7 metadata string key as if it were a financial metric;
  no change to financial formulas or data.
- Locked: no EBITDA, cancellation/target/MT-GT-customer/Top20-SKU/inventory-alert logic unchanged from
  v4.7.2.

## v4.7.2 — MT/GT all-customers MTD, Top20 rebuild, expiry-only inventory sheet

Source: `README_v4_7_2.md` (superseded, merged here, file removed).

- `mtgt_customer_mtd_all`: all GT/MT customers with current-MTD transactions (not limited to Top 5),
  reconciled to `mtd_daily_detail_by_channel`.
- `top_20_sku_by_channel` rebuilt: rank by `sold_qty_mtd` desc, gifts excluded from rank, exact-SKU →
  exact-EAN → `product_id` mapping priority, composite seller SKUs never collapsed to one stock SKU.
- Sheet `06_Ton_kho_Can_date` restricted to `inventory_expiry_alert_skus` (rows with a non-null
  `earliest_expiry_date` from the existing `inventory.v_inventory_alerts_ceo_for_package` view only) — no
  invented 30/60/90-day threshold.

## v4.7.1 — Top20 SKU + inventory sales mapping

Source: `README_v4_7_1.md` (superseded, merged here, file removed).

- Every channel SKU block shows Top 20 best-selling products by MTD sold quantity (gift qty shown
  separately, does not affect rank).
- Multi-lot/expiry inventory for one SKU collapsed to a single row with total current stock summed across
  lots; `stock_qty_sum_all_expiry_dates` must equal `current_stock_qty`.
- New blocks: `top_20_sku_by_channel`, `top_20_sku_rows`, `inventory_sheet_sku_summary`,
  `inventory_sales_mapping_audit`.

## v4.7 — KPI + SKU + inventory

Source: `README_v4_7.md` (superseded, merged here, file removed).

- Adds `target_progress` (MTD actual vs `ops.monthly_sales_targets`, remaining/run-rate/forecast),
  `previous_month_same_day_full_pnl`, `sku_daily_fact`, `inventory` (from
  `inventory.stock_lot_snapshots`), `sku_inventory_summary`, `company_sku_inventory_summary`.
- Locks the 7-sheet Excel render contract (`00_Tong_quan` … `06_Ton_kho_Can_date`) and the mandatory
  top-KPI row (`DOANH SỐ LŨY KẾ | CHỈ TIÊU THÁNG | % THỰC HIỆN | CÒN THIẾU | DỰ BÁO CUỐI THÁNG`).
- Composite marketplace SKUs (e.g. `A+B+C`) flagged `COMPOSITE_REQUIRES_COMPONENT_ALLOCATION`, never
  silently mapped to one inventory product. No fake N/D: confirmed no-sales metrics render `0`, genuinely
  unavailable metrics render `null`. No EBITDA — P&L still ends at `profit`/`LỢI NHUẬN`.

## v4.6 / v4.6.1 — Cancellation semantics + status contract cleanup

Source: `README_v4_6.md` (superseded, merged here, file removed) and `CLAUDE.md`'s own "CEO Daily P&L
v4.6 / v4.6.1 status contract" sections (kept there verbatim as the authoritative current rule text — not
duplicated in full here).

- Per-channel exact-match cancellation semantics: Shopee only exact normalized `Đã hủy`; TikTok only exact
  normalized `Canceled`; Nhanh only exact trimmed `Đã hủy` / `Hệ Thống hủy` / `Khách hủy` / `HVC hủy` — Nhanh
  return statuses (`Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn`) and `Thất bại` are explicitly **not**
  cancellation.
- Nhanh daily P&L promoted to first-class (parent + the 7 exact leaf channels), reconciling by
  construction.
- v4.6.1 clean-up: Unicode NFC + case-insensitive + whitespace-collapsed exact-phrase matching for Nhanh
  cancellation (no accent folding/fuzzy/substring matching); `success_orders` clarified as the
  cross-channel non-cancelled compatibility field, distinct from the Nhanh-only literal
  `successful_orders_exact`.

## MT/GT v4.13 — total-payment revenue patch

Source: `README.txt` (kept at repo root — cited by name in `CLAUDE.md`'s MT/GT critical-business-rules
section as historical contrast against the current v4.23 8%-VAT gross-revenue rule; do not delete).

- MT/GT net revenue taken verbatim from Excel column `"Tổng thanh toán"` (now required), no longer
  recomputed from `quantity × unit_price` and no longer falling back to `"Doanh số bán"`.
- CVC (shipping) lines use `abs("Tổng thanh toán")`.
- Superseded by the current v4.23 rule (`scripts/mt_gt_etl.py:392-411`, adds 8% VAT to gross revenue) —
  the net-revenue-from-`"Tổng thanh toán"` rule described here is still in effect; see `CLAUDE.md`'s
  "MT/GT gross revenue" and "MT/GT net revenue" critical business rules.

## TikTok v4.12 / v4.14 — brand_group-from-filename and partner commission parsing (historical, merged)

Source: `tools/apply_tiktok_v4_12_brand_group_from_filename.py` and
`tools/apply_tiktok_v4_14_partner_commission.py` (removed 2026-08-21 — both one-shot patchers, fully
merged into `scripts/tiktok_etl.py`, zero remaining references once their `CLAUDE.md` citation was
rewritten to describe the rule instead of the filename; safe to remove per the same evidence bar as any
other confirmed-dead file).

- **v4.12**: `brand_group` follows the filename-derived `shop_label` (owner-approved rule); intentionally
  surgical patch that left combo-SKU COGS, the 2026-05-08 platform-fee cutoff, ads/live cost, settlement,
  and packaging logic untouched.
- **v4.14**: replaces `add_partner_commission()` only — TikTok partner affiliate commission parsing —
  preserving all other existing logic (combo SKU COGS, platform fee rules, ads/live parsing, settlement,
  packaging).
- Both rules are permanent, current behavior in `scripts/tiktok_etl.py` today; nothing about the current
  ETL changed by removing the patcher scripts themselves.

## 2026-08-20 — Production cleanup pass (repo hygiene, no business-logic change)

- Removed generated/historical artifacts: `RUN_V4_7*_EXAMPLE.txt`, `SHA256SUMS.txt`,
  `SHA256SUMS_v4_7_3.txt`, all `v4_6_*_self_test.txt` / `v4_7*_offline_self_tests.txt` /
  `v4_7_3_*_self_test.txt`, and the tracked `docs/email_drafts/*.eml` sample.
- Untracked (not deleted from disk) 91 `data/inventory/daily/**` daily snapshot files — these are runtime
  operational input, not source-of-truth config; the sibling `*/2_master_data/`, `*/8_master_data/`, and
  `data/inventory/rules/` directories remain tracked as config source of truth.
- Moved DB password / Gmail App Password out of source into environment variables read via `.env`
  (`etl_common.py`, `api/database.py`, `run_daily_all.ps1`, `run_preview_email.ps1`); added `.env.example`
  and a real `.gitignore`. The old hardcoded values still exist in earlier git history commits already
  pushed to `origin/main` — manual rotation of both secrets is still required, independent of this cleanup
  (history was not rewritten).

## 2026-08-21 — Repo cleanup pass (this pass)

See `docs/PRODUCTION_CLEANUP_MANIFEST.md` for the full file-by-file decision table. Summary: removed 3
confirmed-dead Python files (`scripts/build_ceo_daily_pnl_package_v4_1.py`,
`scripts/validate_ceo_daily_pnl_package_v4_5.py`, `apply_ceo_discount_etl_upgrade.py`) and 5 superseded
per-version README files (merged into this changelog); wrote this file, `REFACTOR_LATER.md`, and a real
project `README.md`; extended `.gitignore`. No business logic, financial formula, or CEO package schema
was touched.

## 2026-08-21 — Legacy runner/installer cleanup pass

Windows Task Scheduler was verified on the production machine (see
`docs/CHECK_WINDOWS_TASK_SCHEDULER.md`) and found to reference none of this repo's `.ps1` files. Combined
with a final repo-wide reference scan, the following were deleted:

- 15 legacy versioned `.ps1` runners/installers: `run_daily_pnl_v4.ps1`, `run_daily_pnl_v4_6.ps1`,
  `run_daily_pnl_v4_6_1.ps1`, `run_daily_pnl_v4_7.ps1`, `run_daily_pnl_v4_7_1.ps1`,
  `run_daily_pnl_v4_7_2.ps1`, `run_daily_pnl_v4_7_3.ps1`, `run_daily_inventory_and_ceo_v4_7.ps1`,
  `_v4_7_1.ps1`, `_v4_7_2.ps1`, `_v4_7_3.ps1`, `install_ceo_daily_pnl_v4_6.ps1`, `_v4_7.ps1`, `_v4_7_1.ps1`,
  `_v4_7_2.ps1`. The 5 current entrypoints (`run_daily_all.ps1`, `run_daily_pnl_v4_7_4.ps1`,
  `install_ceo_daily_pnl_v4_7_4.ps1`, `run_preview_email.ps1`, `run_send_ceo_daily_individual.ps1`) were
  not touched.
- 3 now-orphaned Python dependencies of the above: `scripts/build_ceo_daily_pnl_package_v4_0.py`,
  `tools/apply_ceo_daily_pnl_v4_6_upgrade.py`, `tools/apply_ceo_daily_pnl_v4_6_1_upgrade.py`.
- 2 already-applied historical TikTok patchers, after their `CLAUDE.md` citation was rewritten to describe
  the merged rules instead of depending on the filenames (see "TikTok v4.12 / v4.14" entry above):
  `tools/apply_tiktok_v4_12_brand_group_from_filename.py`, `tools/apply_tiktok_v4_14_partner_commission.py`.

Added `docs/CHECK_WINDOWS_TASK_SCHEDULER.md` (the verification runbook used above). No ETL, filter,
cancellation, gift, discount, fee, COGS, package-schema, or DB-schema logic was changed. The inventory
orchestration gap noted in `REFACTOR_LATER.md` §1.3 is unresolved and now slightly more visible — confirm
with ops how inventory data loads before the next v4.7.4 report run.
