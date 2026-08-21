# Production Cleanup Inventory — hh-lpm-etl

- Repo: `nguyentlevu203-create/hh-lpm-etl`
- HEAD at audit time: `14db86aac9e32eb28b2fd28b86377bc8c2e881ac` (branch `main`)
- Date: 2026-08-20
- Tracked files audited: 239 (`git ls-files`)
- Methodology: `git ls-files` for the full tracked set, then per-file dependency proof via
  `grep -rn` across `*.py *.ps1 *.sql *.md *.txt *.csv`, `importlib.util.spec_from_file_location`
  chain tracing (`BASE_FILENAME`/`BASE_BUILDER`/`load_module` call sites), and direct read of
  entry-point scripts (`run_daily_all.ps1`, `api/main.py`, `run_daily_pnl_v4_7_4.ps1`,
  `install_ceo_daily_pnl_v4_7_4.ps1`). No file was deleted, moved, or edited except this doc.

## ⚠️ Findings that outrank routine cleanup (read before acting on any category below)

1. **`scripts/apply_shopee_gift_patch_v4_7_4.py` has NOT been applied.** Its precondition text
   `SHOPEE_ETL_VERSION = "v4.11.3_windows_locked_file_archive_retry"` is absent from both the
   committed HEAD `scripts/shopee_etl.py` and the current working tree (working tree currently
   reads `v4.50_exact_cancel_gross_all_status`). `classify_shopee_group`/`shopee_gift_classifier_v4_7_4`
   is not imported anywhere in `shopee_etl.py`. The v4.7.4 gift-classification logic
   (`scripts/shopee_gift_classifier_v4_7_4.py`, `config/shopee_promotion_gift_rules_v4_7_4.csv`)
   is committed but **not wired into the running ETL**. Do not classify this patcher as "already
   applied, safe legacy" — it is pending, and deleting it or its target module would strand the
   v4.7.4 gift feature.
2. **`scripts/shopee_etl.py` (and `nhanh_etl.py`, `tiktok_etl.py`,
   `validate_ceo_daily_pnl_package_v4_7.py`) have uncommitted local changes** (`git status`
   shows `M`). The committed HEAD `shopee_etl.py` blob (`7da3c8b9...`) is the *pre*-exact-cancel,
   *pre*-gross-all-status version; the exact-cancel + gross-all-status logic (from
   `tools/apply_marketplace_exact_cancel_patch_v4_5.py` / `tools/apply_shopee_gross_all_status_patch_v4_1.py`)
   is only in the **working tree**, uncommitted. A fresh clone of `origin/main` today would get the
   *older* behavior. This directly blocks the user's stated goal ("fresh clone chạy production mà
   không cần chạy patcher thủ công") and should be resolved (commit the working-tree state, or
   re-run/verify the patchers) before declaring the repo production-ready. Out of scope for this
   read-only Phase 1 — flagged for the coordinator.
3. **Inventory pipeline orchestration gap**: `scripts/run_daily_inventory_pipeline.py` (→
   `inventory_daily_filter.py`, `import_reorder_alert_rules.py`) is only invoked by the legacy
   `run_daily_inventory_and_ceo_v4_7*.ps1` wrappers (v4_7 through v4_7_3). There is no
   `run_daily_inventory_and_ceo_v4_7_4.ps1`, and `run_daily_pnl_v4_7_4.ps1` only *consumes*
   inventory data (`--allow-missing-inventory` flag) — it does not populate it. Confirm with the
   user how inventory snapshots currently get loaded before touching any file in this group.

---

## PRODUCTION_RUNTIME

| Path | Evidence | Notes |
|---|---|---|
| `scripts/nhanh_etl.py` | `run_daily_all.ps1:53` | Channel ETL; has uncommitted changes, see finding #2 |
| `scripts/shopee_etl.py` | `run_daily_all.ps1:56` | Channel ETL; has uncommitted changes, see findings #1–2 |
| `scripts/tiktok_etl.py` | `run_daily_all.ps1:59` | Channel ETL; has uncommitted changes |
| `scripts/mt_gt_etl.py` | `run_daily_all.ps1:62` | Channel ETL, reads `data/mt_gt/2_master_data/` (`scripts/mt_gt_etl.py:47`) |
| `scripts/send_staff_scorecard_emails.py` | `run_daily_all.ps1:66`; also `load_module(CEO_EMAIL_SCRIPT,...)` in v2_10 control tower builder | Daily staff email + reused by CEO Control Tower builder |
| `etl_common.py` | imported by all 4 channel ETLs + others (`grep -rl "from etl_common import" scripts/*.py` → 7 files) | Shared DB/env/cost-map helper module |
| `main.py` | project root FastAPI entry (`uvicorn main:app` per CLAUDE.md) | loads `api/main.py` |
| `api/main.py` | `api/main.py:3,22-26` includes all 5 routers | FastAPI app entry |
| `api/database.py` | imported by API routers (read path per CLAUDE.md) | |
| `api/routers/ceo.py`, `mt_gt.py`, `nhanh.py`, `shopee.py`, `tiktok.py` | `api/main.py:3,22-26` | All 5 mounted |
| `api/routers/__init__.py` | package marker, empty file | |
| `scripts/build_ceo_daily_pnl_package_v4_7_4.py` | top of importlib chain; current per `BUNDLE_MANIFEST.json` `"release": "v4.7.4"` | CURRENT CEO Daily P&L builder |
| `scripts/validate_ceo_daily_pnl_package_v4_7_4.py` | top of validator importlib chain | CURRENT validator |
| `run_daily_pnl_v4_7_4.ps1` | invokes `build/validate_..._v4_7_4.py` | CURRENT orchestrator |
| `install_ceo_daily_pnl_v4_7_4.ps1` | self-test wrapper for the v4_7_4 chain | CURRENT installer/self-test |
| `scripts/build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible.py` | CLAUDE.md "Current production entry points"; `load_module()` calls at lines 511/515/545 | Manual/out-of-band current entry point |
| `scripts/send_ceo_control_tower_report.py` | CLAUDE.md, emails the v2_10 package | Manual entry point |
| `scripts/build_ceo_growth_package_v3_3_week_month.py` | loaded by `build_ceo_daily_pnl_package_v4_5.py:69,246` (`BASE_BUILDER`) **and** CLAUDE.md manual entry point | Dual role: load-bearing dependency of v4_7_4 chain AND standalone weekly/monthly builder |
| `scripts/send_ceo_growth_week_month_excel_report.py` | CLAUDE.md current sender | Manual entry point |
| `scripts/send_ceo_daily_excel_individual.py` | CLAUDE.md; invoked by `run_send_ceo_daily_individual.ps1` | Manual entry point |
| `run_send_ceo_daily_individual.ps1` | references `config/ceo_daily_email_recipients.csv` | Wrapper for individual sender |
| `config/ceo_daily_email_recipients.csv` | `run_send_ceo_daily_individual.ps1` | Recipient list |
| `import_costs.py` | documented in CLAUDE.md as the loader for `dim.product_costs`; no `.ps1`/`.py` caller found (`grep` → CLAUDE.md only) | Manual operational tool — no automated wiring found; confirm ops still runs it manually |
| `scripts/rebind_stale_product_aliases_v4_7_4_HF2.py` | `README_HF2.txt` step-by-step ops runbook | Manual data-migration tool (current, HF2) |
| `scripts/import_product_sku_aliases_v4_7_4.py` | `README_HF2.txt` step 3; `BUNDLE_MANIFEST.json` file list | Manual data-migration tool (current) |
| `scripts/export_unmapped_sales_skus_v4_7_4.py` | `BUNDLE_MANIFEST.json` file list; produced `qa/unmapped_sales_skus_2026-08-18_after_v4_7_4.csv` | Manual QA/export tool (current) |

## LOAD_BEARING_DEPENDENCY

| Path | Evidence | Notes |
|---|---|---|
| `scripts/build_ceo_daily_pnl_package_v4_7_3.py` | loaded by v4_7_4 (`BASE_FILENAME` at `build_ceo_daily_pnl_package_v4_7_4.py:42`) | |
| `scripts/build_ceo_daily_pnl_package_v4_7_2.py` | loaded by v4_7_3 (`:46`) | |
| `scripts/build_ceo_daily_pnl_package_v4_7_1.py` | loaded by v4_7_2 (`:38`) | |
| `scripts/build_ceo_daily_pnl_package_v4_7.py` | loaded by v4_7_1 (`:41`) | |
| `scripts/build_ceo_daily_pnl_package_v4_6_1.py` | loaded by v4_7 (`:53`) | |
| `scripts/build_ceo_daily_pnl_package_v4_6.py` | loaded by v4_6_1 (`:45`) | |
| `scripts/build_ceo_daily_pnl_package_v4_5.py` | loaded by v4_6 (`:65`); itself loads `build_ceo_growth_package_v3_3_week_month.py` (`:69,246`) | Chain root |
| `scripts/validate_ceo_daily_pnl_package_v4_7_3.py` | loaded by validate_v4_7_4 (`:19`) | |
| `scripts/validate_ceo_daily_pnl_package_v4_7_2.py` | loaded by validate_v4_7_3 (`:19`) | |
| `scripts/validate_ceo_daily_pnl_package_v4_7_1.py` | loaded by validate_v4_7_2 (`:17`) | |
| `scripts/validate_ceo_daily_pnl_package_v4_7.py` | loaded by validate_v4_7_1 (`:17`) | Also has uncommitted local changes — see git status |
| `scripts/validate_ceo_daily_pnl_package_v4_6_1.py` | loaded by validate_v4_7 (`:18`) | |
| `scripts/validate_ceo_daily_pnl_package_v4_6.py` | loaded by validate_v4_6_1 (`:16`) | Chain root (no `BASE_FILENAME`, confirmed by grep) |
| `scripts/build_ceo_control_tower_package_v2_scorecard.py` | `load_module(BASE_SCORECARD_BUILDER,...)`, `BASE_SCORECARD_BUILDER` = this path at v2_10 line 18 | Legacy standalone but load-bearing, per CLAUDE.md |
| `scripts/build_ceo_control_tower_package_v2_8_samples.py` | `SAMPLE_BUILDER` = this path at v2_10 line 19 | Legacy standalone but load-bearing, per CLAUDE.md |
| `scripts/inventory_daily_filter.py` | called by `scripts/run_daily_inventory_pipeline.py` | See finding #3 for orchestration gap |
| `scripts/import_reorder_alert_rules.py` | called by `scripts/run_daily_inventory_pipeline.py` | See finding #3 |
| `data/mt_gt/2_master_data/*.xlsx, ads.csv` (6 files) | `scripts/mt_gt_etl.py:47` `MASTER_DIR` | See CONFIG_SOURCE_OF_TRUTH below (also fits this category; listed once there) |

## CONFIG_SOURCE_OF_TRUTH

| Path | Evidence | Notes |
|---|---|---|
| `"Giá nhập 25.06.2026.xlsx"` | CLAUDE.md: "populated by `import_costs.py`"; feeds `dim.product_costs`, read by `etl_common.load_cost_map()` | Has uncommitted changes (`M` in git status) |
| `data/mt_gt/2_master_data/*.xlsx`, `ads.csv` (6 files: Bảng giá vốn, Kênh, Nhân Viên, Phân loại kênh, ads.csv, khách hàng) | `scripts/mt_gt_etl.py:47` `MASTER_DIR = BASE_IN / "2_master_data"` | 3 of these 6 show `M` (Nhân Viên.xlsx, ads.csv, khách hàng.xlsx) |
| `data/tiktok/8_master_data/*.xlsx` (3 files: Bảng giá vốn, Nhóm Hàng, Phí Book) | `scripts/tiktok_etl.py:63,705` | |
| `data/inventory/rules/"Cảnh báo đặt hàng.xlsx"` (1 file) | consumed by `scripts/import_reorder_alert_rules.py` (name match; not exhaustively line-traced) | |
| `config/bundle_component_map_v4_7_4.csv` | `BUNDLE_MANIFEST.json`; consumed by v4_7_4 builder/importer chain (file naming + manifest membership; not individually line-traced in this pass) | |
| `config/product_sku_alias_master_v4_7_4.csv` | `README_HF2.txt` steps 1–3; `scripts/import_product_sku_aliases_v4_7_4.py --file` arg pattern | |
| `config/product_sku_alias_review_v4_7_4.csv` | `BUNDLE_MANIFEST.json` file list | |
| `config/shopee_promotion_gift_rules_v4_7_4.csv` | `scripts/apply_shopee_gift_patch_v4_7_4.py:45` default path `SHOPEE_GIFT_RULES_FILE` | Only becomes "live" once finding #1's patch is applied |
| `config/ceo_daily_email_recipients.csv` | `run_send_ceo_daily_individual.ps1` | |

## MIGRATION

| Path | Evidence | Notes |
|---|---|---|
| `sql/migrations/hh_control_tower_db_upgrade_v2_12_nhanh_shipping_bo_return_rules.sql` | CLAUDE.md cites it directly for the Nhanh shipping rule | Keep permanently (DB history) |
| `sql/migrations/hh_control_tower_db_upgrade_v2_13_ceo_channel_discounts.sql` | 0 references found via grep (incl. CLAUDE.md) | Keep permanently — migrations are DB history regardless of current reference count |
| `sql/seed/seed_product_master_v1.sql` | referenced by CLAUDE.md ("Missing base schema DDL" section) | |
| `hh_growth_week_month_views_v3_0.sql` | referenced by CLAUDE.md ("Reporting architecture") as the base for `control_tower.*`/`mart.*` views | Root-level, not under `sql/` — naming inconsistency worth a Phase 5 note |

## TEST_FIXTURE

None found. Confirms CLAUDE.md's statement that there is no automated test suite. `sql/checks/check_shopee_scorecard_v2_7.sql` is a manual validation query (see MIGRATION-adjacent note below) rather than an automated fixture.

| Path | Evidence | Notes |
|---|---|---|
| `sql/checks/check_shopee_scorecard_v2_7.sql` | CLAUDE.md: "the only validation artifact...manual query hardcoded to one date" | Not a MIGRATION and not an automated TEST_FIXTURE — closest fit is DOCUMENTATION_CURRENT/manual-tool; listed here for visibility only, not double-counted |

## DOCUMENTATION_CURRENT

| Path | Evidence | Notes |
|---|---|---|
| `CLAUDE.md` | this file | Needs Phase 5 update: still frames v4_7_3 as current in places |
| `docs/CEO_DAILY_PNL_V4_7_4_CONTRACT.md` | newest contract doc, matches `BUNDLE_MANIFEST.json` release | |
| `README_v4_7_4.md` | untracked at repo root, describes v4.7.4 bundle | Should arguably move under `docs/` in Phase 5, not delete |
| `README_HF2.txt` | describes the current HF2 alias-rebind runbook | |
| `SHA256SUMS_v4_7_4.txt` | checksum manifest for the current bundle | Borderline GENERATED_ARTIFACT (see below) but still actively useful as a current-release integrity record |
| `BUNDLE_MANIFEST.json` | `"release": "v4.7.4"` | **Stale**: `"base": "v4.7.3 HF1"` and file list omits the 4 config CSVs added after this file was last edited — Phase 5 doc-drift, not a Phase 1 action |
| `docs/GITHUB_MAIN_REVIEW.md` | references `run_daily_inventory_pipeline.py`, still-relevant subsystem | |

## DOCUMENTATION_LEGACY

| Path | Evidence | Notes |
|---|---|---|
| `docs/CEO_DAILY_PNL_V4_7_3_CONTRACT.md` | superseded by V4_7_4 contract | Kept per CLAUDE.md's own stated policy of keeping prior-step contract docs |
| `docs/CEO_DAILY_PNL_V4_7_2_CONTRACT.md` | superseded | |
| `docs/CEO_DAILY_PNL_V4_7_1_CONTRACT.md` | superseded | |
| `docs/CEO_DAILY_PNL_V4_7_CONTRACT.md` | superseded | |
| `docs/CEO_DAILY_PNL_V4_6_CONTRACT.md` | superseded | |
| `docs/QA_V4_7.md`, `docs/QA_V4_7_1.md`, `docs/QA_V4_7_2.md` | offline QA logs for superseded releases | |
| `README.txt` | CLAUDE.md explicitly cites it for MT/GT v4.13 vs current v4.23 contrast | Do not delete — CLAUDE.md load-bearing citation |
| `README_v4_6.md`, `README_v4_7.md`, `README_v4_7_1.md`, `README_v4_7_2.md`, `README_v4_7_3.md` | superseded release notes | |
| `README.md` | currently titled "CEO Daily Gmail Individual Sender v1", **not a general project README** | Phase 5 explicitly calls for turning this into the main README — flagging the mismatch, not fixing it here |

## GENERATED_ARTIFACT

| Path | Evidence | Notes |
|---|---|---|
| `RUN_V4_7_EXAMPLE.txt`, `RUN_V4_7_1_EXAMPLE.txt`, `RUN_V4_7_3_EXAMPLE.txt` | content = literal copy-paste command examples/output | |
| `SHA256SUMS.txt`, `SHA256SUMS_v4_7_3.txt` | content = checksum listing of a past release | Superseded by `SHA256SUMS_v4_7_4.txt` |
| `v4_6_builder_self_test.txt`, `v4_6_patcher_self_test.txt`, `v4_6_validator_self_test.txt` | content = "SELF-TEST PASSED" + JSON dump | |
| `v4_7_offline_self_tests.txt`, `v4_7_2_offline_self_tests.txt`, `v4_7_3_builder_self_test.txt`, `v4_7_3_offline_self_tests.txt`, `v4_7_3_validator_self_test.txt` | same pattern | |
| `qa/unmapped_sales_skus_2026-08-18_after_v4_7_4.csv` | output of `scripts/export_unmapped_sales_skus_v4_7_4.py` | Current-release QA output — candidate for `.gitignore` rather than deletion (Phase 3/4 call) |
| `qa/v4_7_4_offline_test_summary.txt` | self-test log for current release | Same as above |
| `docs/email_drafts/ceo_control_tower_email_2026-06-15_20260616_112259.eml` | timestamped email draft output | Matches the pattern of `outbox/email_drafts/` which CLAUDE.md's `.gitignore` section says is *meant* to be ignored — this one is tracked, likely predates the 2026-08-19 `.gitignore` addition |

## ONE_SHOT_PATCHER

| Path | Evidence | Notes |
|---|---|---|
| `tools/apply_tiktok_v4_12_brand_group_from_filename.py` | CLAUDE.md explicit "do not rerun" | Already applied per CLAUDE.md |
| `tools/apply_tiktok_v4_14_partner_commission.py` | CLAUDE.md explicit "do not rerun"; `add_partner_commission()` present in `scripts/tiktok_etl.py:900` | Already applied |
| `tools/apply_ceo_daily_pnl_v4_6_upgrade.py` | its `MARKER = "CEO_DAILY_PNL_V4_6_STATUS_CONTRACT"` block IS present in `CLAUDE.md:239-249` | Already applied — target is CLAUDE.md, not channel ETL source, despite the docstring's "channel ETL classifiers" wording |
| `tools/apply_ceo_daily_pnl_v4_6_1_upgrade.py` | its `MARKER = "CEO_DAILY_PNL_V4_6_1_CLEANUP_CONTRACT"` block IS present in `CLAUDE.md:251-264` | Already applied |
| `tools/apply_marketplace_exact_cancel_patch_v4_5.py` | exact-cancel normalization logic present in **working-tree** `shopee_etl.py` (uncommitted) | Applied to working tree only — see finding #2, not yet committed |
| `tools/apply_shopee_gross_all_status_patch_v4_1.py` | `SHOPEE_ETL_VERSION` in working tree = `"v4.50_exact_cancel_gross_all_status"` reflects this patch | Same as above — applied but uncommitted |
| `scripts/apply_shopee_gift_patch_v4_7_4.py` | precondition text absent from both HEAD and working tree; classifier not imported | **NOT applied** — see finding #1. Do not treat as routine "safe legacy patcher" |
| `apply_ceo_discount_etl_upgrade.py` (root) | 0 references anywhere | Confirmed one-shot, target unclear from static grep alone — see LEGACY_UNREFERENCED |
| `tools/check_ceo_daily_pnl_v4_7_repo.py` | read-only readiness checker, not a patcher (`inspect()` only reads files, no writes found in header scan) | Diagnostic tool, not "applied/unapplied" — kept for ops readiness checks |

## LEGACY_UNREFERENCED

| Path | Evidence | Notes |
|---|---|---|
| `scripts/build_ceo_daily_pnl_package_v4_1.py` | `grep -rln "build_ceo_daily_pnl_package_v4_1\b"` → only CLAUDE.md (prose mention) + itself; v4_5's importlib chain loads nothing before itself except `build_ceo_growth_package_v3_3_week_month.py` | Confirmed dead — superseded by v4_5 |
| `scripts/validate_ceo_daily_pnl_package_v4_5.py` | `validate_ceo_daily_pnl_package_v4_6.py` has no `BASE_FILENAME` (confirmed via grep) — chain stops at v4_6, never reaches v4_5 | Confirmed dead |
| `apply_ceo_discount_etl_upgrade.py` (root) | 0 references in any `*.py *.ps1 *.md *.txt` | Confirmed dead |
| `scripts/build_ceo_daily_pnl_package_v4_0.py` | only caller is `run_daily_pnl_v4.ps1` (itself legacy/unreferenced, see below) | See note — technically has one caller, but that caller is itself orphaned; kept here rather than LOAD_BEARING since the whole chain is disconnected from anything current |
| `run_daily_pnl_v4.ps1` | 0 external callers found; superseded by `run_daily_pnl_v4_7_4.ps1` | Only reference to `build_ceo_daily_pnl_package_v4_0.py` in the repo |

## UNKNOWN_REVIEW_REQUIRED

| Path | Reason |
|---|---|
| `scripts/apply_shopee_gift_patch_v4_7_4.py` | Not yet applied (finding #1) — status is "pending," not "legacy." Needs explicit user decision: apply it, or is it superseded by a different mechanism? |
| `scripts/shopee_gift_classifier_v4_7_4.py` | Only consumer is the not-yet-applied patcher above — currently dead code in the running ETL, but clearly the intended current logic |
| `scripts/build_ceo_daily_cumulative_package.py` | 0 references anywhere; not mentioned in CLAUDE.md's entry-point list; unclear if this is an abandoned experiment or an undocumented manual report ops still runs |
| `scripts/import_sample_orders_v2.py` | 0 external references; writes to `control_tower.sample_orders` — unclear if this is a QA/demo tool or a still-used manual utility |
| `run_daily_pnl_v4_6.ps1`, `run_daily_pnl_v4_6_1.ps1`, `run_daily_pnl_v4_7.ps1`, `run_daily_pnl_v4_7_1.ps1`, `run_daily_pnl_v4_7_2.ps1`, `run_daily_pnl_v4_7_3.ps1` | No external caller found, but each targets a builder/validator pair that IS still load-bearing (v4_5 through v4_7_3) — the runner scripts themselves look orphaned, but can't rule out manual/muscle-memory use by ops outside repo evidence |
| `run_daily_inventory_and_ceo_v4_7.ps1`, `_v4_7_1.ps1`, `_v4_7_2.ps1`, `_v4_7_3.ps1` | Same pattern — orphaned per repo grep, but see finding #3 (inventory orchestration gap); deleting before confirming the replacement path risks breaking inventory population entirely |
| `install_ceo_daily_pnl_v4_6.ps1`, `_v4_7.ps1`, `_v4_7_1.ps1`, `_v4_7_2.ps1` | Superseded by `install_ceo_daily_pnl_v4_7_4.ps1`; no external caller, but same "ops muscle memory" caveat as above |
| `run_preview_email.ps1` | Not traced in this pass — referenced by CLAUDE.md's Security rules section by name only ("never expose secrets in ... `run_preview_email.ps1`") but its call chain wasn't verified |
| `sql/audit_ceo_daily_pnl_v4_6_1.sql`, `sql/audit_ceo_daily_pnl_v4_6_status_contract.sql`, `sql/audit_ceo_daily_pnl_v4_7_2.sql`, `sql/audit_ceo_daily_pnl_v4_7_sources.sql` | Referenced only from README_v4_6.md/README_v4_7.md/tools patchers (legacy docs) — manual audit queries, same category as `check_shopee_scorecard_v2_7.sql` which CLAUDE.md explicitly protects; not independently confirmed still-run |
| `sql/audit_cogs_split_v4_5.sql`, `sql/audit_marketplace_exact_cancel_v4_5.sql`, `sql/audit_nhanh_leaf_v4_5.sql` | 0 references found; same manual-audit-query category, kept per rule 11 |
| `sql/audit_v4_7_4_product_mapping_and_gift.sql` | Part of the current v4.7.4 bundle per `BUNDLE_MANIFEST.json`, but not line-traced to a specific caller in this pass |
| `sql/upgrade_ceo_daily_pnl_v4_7_optional_targets.sql` | Referenced by `install_ceo_daily_pnl_v4_7.ps1` (legacy installer) only |
| `data/inventory/daily/**` (91 files, 2026-07-01 to 2026-08-18) | Raw daily snapshot exports, tracked in git. CLAUDE.md's data/ note says data/ is "intentionally tracked (small, reference/audit value)" but doesn't specifically bless daily operational snapshots this granular — flagged for Phase 4 (coordinator's call, not deleted here) |
| `data/sample_orders/2026-06/TIKTOK/*.xlsx` (1 file) | Only consumer is `scripts/import_sample_orders_v2.py`, itself UNKNOWN_REVIEW_REQUIRED above |
| `hh_growth_week_month_views_v3_0.sql` | Root-level placement (not under `sql/`) is inconsistent with the rest of the SQL layout — functionally MIGRATION-like per CLAUDE.md, but the location itself is worth a Phase 5 note |

---

## Phase 1 summary for coordinator

**Counts by category** (approximate; data/ groups counted as 1 row per group, not per file):
PRODUCTION_RUNTIME 26 · LOAD_BEARING_DEPENDENCY 17 (3 overlap-noted) · CONFIG_SOURCE_OF_TRUTH 9 rows (~19 files) ·
MIGRATION 4 · TEST_FIXTURE 0 (1 note) · DOCUMENTATION_CURRENT 7 · DOCUMENTATION_LEGACY 13 ·
GENERATED_ARTIFACT 12 rows (~13 files) · ONE_SHOT_PATCHER 9 · LEGACY_UNREFERENCED 5 ·
UNKNOWN_REVIEW_REQUIRED 21 rows (~120 files, dominated by the 91-file inventory/daily group).

**Confirmed LEGACY_UNREFERENCED (only these 5 meet the strict "zero references, not current, not load-bearing" bar):**
1. `scripts/build_ceo_daily_pnl_package_v4_1.py`
2. `scripts/validate_ceo_daily_pnl_package_v4_5.py`
3. `apply_ceo_discount_etl_upgrade.py` (root)
4. `scripts/build_ceo_daily_pnl_package_v4_0.py`
5. `run_daily_pnl_v4.ps1`

(#4 and #5 are new refinements vs. the earlier ad-hoc audit — they form a self-contained orphaned pair: the only caller of #4 is #5, and #5 has no caller of its own.)

**Everything else that looks deletable is UNKNOWN_REVIEW_REQUIRED per rule 11** — the largest blocks are: 10 orphaned `.ps1` runners (v4_6 through v4_7_3 chain) kept alive only by the "might still be run manually by ops" caveat, ~8 manual SQL audit queries, and the 91-file `data/inventory/daily/` snapshot tree (a Phase 4 question, not Phase 3).

**Do not act on the three ⚠️ findings above as routine cleanup** — they are correctness/completeness gaps (unapplied Shopee gift patch, uncommitted channel-ETL drift, inventory orchestration gap), not dead files.
