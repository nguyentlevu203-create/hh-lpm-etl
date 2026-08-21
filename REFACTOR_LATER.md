# Refactor / follow-up backlog — hh-lpm-etl

Recommendations only. Nothing in this file was executed as part of the 2026-08-21 cleanup pass — each item
either changes business-relevant wiring, needs a human decision (Task Scheduler / ops confirmation), or is
explicitly out of scope for a "no business logic change" cleanup. See `docs/PRODUCTION_CLEANUP_MANIFEST.md`
for the file-by-file evidence this is based on.

## 1. Correctness/completeness gaps (fix before more cleanup, not cleanup items themselves)

1. **`scripts/apply_shopee_gift_patch_v4_7_4.py` has not been applied.** Its precondition (audited GitHub
   blob `7da3c8b9...`) no longer matches the working-tree `scripts/shopee_etl.py`
   (`v4.50_exact_cancel_gross_all_status`), so it now fails closed and cannot even run without a fresh
   audit + rewrite of its anchor text. Until this is resolved, v4.7.4's gift-classification fields
   (`gift_sku_mtd_by_channel`, etc.) do not reflect real Shopee gift data. Do not delete the patcher or
   `scripts/shopee_gift_classifier_v4_7_4.py` — they are the only implementation of the intended current
   logic, just not wired in yet.
2. **`scripts/shopee_etl.py`, `nhanh_etl.py`, `tiktok_etl.py`, `validate_ceo_daily_pnl_package_v4_7.py`
   have uncommitted working-tree changes** (exact-cancel/gross-all-status logic from
   `tools/apply_marketplace_exact_cancel_patch_v4_5.py` / `tools/apply_shopee_gross_all_status_patch_v4_1.py`,
   per `CLAUDE.md`). A fresh clone of `origin/main` today runs older behavior. This is why those two
   `tools/apply_*` patchers must stay in the repo (see §3) — they are currently the only record of how to
   reproduce the working-tree state. Recommend: review the working-tree diff, then commit it, before
   treating "fresh clone = current production" as true.
3. **Inventory orchestration gap**: `scripts/run_daily_inventory_pipeline.py` is only invoked by
   `run_daily_inventory_and_ceo_v4_7.ps1` … `_v4_7_3.ps1` (all legacy/orphaned per §2 below). There is no
   `run_daily_inventory_and_ceo_v4_7_4.ps1`, and `run_daily_pnl_v4_7_4.ps1` only *consumes* inventory data
   (`--allow-missing-inventory`), it does not populate it. Confirm with ops how inventory snapshots
   currently get loaded for the v4.7.4 report before deleting anything in the `run_daily_inventory_and_ceo_*`
   family.

## 2. Versioned PowerShell — RESOLVED 2026-08-21

Windows Task Scheduler was checked on the production machine (searched for `run_daily`, `run_daily_pnl`,
`run_daily_inventory`, `install_ceo`, plus a broader sweep for `ETL_production_v3_exact_codes` /
`powershell` / `pwsh` / `python` / `cmd.exe` / `.bat` / `.cmd`) — zero results referencing this repo, only
standard Windows system tasks. Combined with a final repo-wide reference scan (every remaining reference
was either documentation-only or an intra-legacy-set call where both ends were also being deleted), the
following 15 files were deleted:

`run_daily_pnl_v4.ps1`, `run_daily_pnl_v4_6.ps1`, `run_daily_pnl_v4_6_1.ps1`, `run_daily_pnl_v4_7.ps1`,
`run_daily_pnl_v4_7_1.ps1`, `run_daily_pnl_v4_7_2.ps1`, `run_daily_pnl_v4_7_3.ps1`,
`run_daily_inventory_and_ceo_v4_7.ps1`, `_v4_7_1.ps1`, `_v4_7_2.ps1`, `_v4_7_3.ps1`,
`install_ceo_daily_pnl_v4_6.ps1`, `_v4_7.ps1`, `_v4_7_1.ps1`, `_v4_7_2.ps1`.

Their now-orphaned Python targets were deleted in the same pass: `scripts/build_ceo_daily_pnl_package_v4_0.py`,
`tools/apply_ceo_daily_pnl_v4_6_upgrade.py`, `tools/apply_ceo_daily_pnl_v4_6_1_upgrade.py`.

**Kept, unconditionally current**: `run_daily_all.ps1`, `run_daily_pnl_v4_7_4.ps1`,
`install_ceo_daily_pnl_v4_7_4.ps1`, `run_preview_email.ps1`, `run_send_ceo_daily_individual.ps1`.

Remaining future recommendation (not done): consolidate the 5 kept entrypoints into the two files
`CLAUDE.md` already suggests, `scripts/run_daily.ps1` and `scripts/bootstrap.ps1` — a genuine refactor, out
of scope for a cleanup pass.

⚠️ Gap #3 above (inventory orchestration) is **still open** — deleting `run_daily_inventory_and_ceo_v4_7*.ps1`
removed the only in-repo path that populated inventory snapshots ahead of a v4.7.x build. This was a
deliberate, confirmed-safe deletion (Task Scheduler doesn't call it either), but it means: **confirm with
ops how inventory data is actually loaded before the next v4.7.4 report run** — this cleanup pass did not
create a replacement, only removed the dead one.

## 3. One-shot patchers — RESOLVED 2026-08-21 (two of three)

`tools/apply_tiktok_v4_12_brand_group_from_filename.py`, `tools/apply_tiktok_v4_14_partner_commission.py`
— **deleted**. Both already-applied, zero other references; `CLAUDE.md`'s "do not rerun" paragraph was
rewritten first to describe the two merged rules (brand_group-from-filename, partner commission parsing)
without depending on the patcher filenames existing, and the historical patch record was preserved in
`CHANGELOG.md` before deletion.

`tools/apply_ceo_daily_pnl_v4_6_upgrade.py`, `tools/apply_ceo_daily_pnl_v4_6_1_upgrade.py` — **deleted**,
together with the legacy installers that were their only callers (§2).

`tools/apply_marketplace_exact_cancel_patch_v4_5.py`, `tools/apply_shopee_gross_all_status_patch_v4_1.py`
— **not touched, still not a cleanup candidate**: see gap #2 above. Keep until the working-tree ETL changes
they produced (B3/B4, still held/uncommitted) are resolved.

## 4. Already-flattened conclusion: CEO Daily P&L v4.5 → v4.7.4 builder/validator chain

Confirmed still fully load-bearing end to end (`v4_7_4` → `v4_7_3` → … → `v4_5` for the builder; `v4_7_4` →
… → `v4_6` for the validator). Every intermediate file is imported via `importlib` by the next step up.
**Not touched in this pass**, per explicit instruction. Recommendation for a dedicated future phase: flatten
into a single current builder/validator once the chain's behavior is captured by tests, so 8 builder files
+ 7 validator files collapse to 1 each — this is a multi-day project-shaped task, not a cleanup edit.

## 5. Control Tower package builder chain

`build_ceo_control_tower_package_v2_scorecard.py` and `build_ceo_control_tower_package_v2_8_samples.py`
are both dynamically `importlib`-loaded by the current
`build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible.py` (`BASE_SCORECARD_BUILDER`
/ `SAMPLE_BUILDER`). **Not retired in this pass.** Recommendation: same flattening treatment as §4, once
the v2_10 output is captured by a regression fixture.

## 6. Unclear-purpose files (REVIEW — no action taken)

- `scripts/build_ceo_daily_cumulative_package.py` — zero references anywhere, not in CLAUDE.md's
  entry-point list. Could be an abandoned experiment or an undocumented manual report ops still runs.
  Needs a direct answer from whoever runs manual reports before it can be classified DELETE or KEEP.
- `scripts/import_sample_orders_v2.py` and its only input,
  `data/sample_orders/2026-06/TIKTOK/*.xlsx` — zero external references, writes to
  `control_tower.sample_orders`. Unclear if this is a QA/demo tool or a still-used manual utility.
- `sql/audit_ceo_daily_pnl_v4_6_1.sql`, `sql/audit_ceo_daily_pnl_v4_6_status_contract.sql`,
  `sql/audit_ceo_daily_pnl_v4_7_2.sql`, `sql/audit_ceo_daily_pnl_v4_7_sources.sql`,
  `sql/audit_cogs_split_v4_5.sql`, `sql/audit_marketplace_exact_cancel_v4_5.sql`,
  `sql/audit_nhanh_leaf_v4_5.sql` — manual audit queries, same category as the CLAUDE.md-protected
  `sql/checks/check_shopee_scorecard_v2_7.sql`. Not independently confirmed still-run; treated as
  DB-history/audit-value and left alone.

## 7. Housekeeping already true today (no action needed, listed for completeness)

- No dependency lock file (`requirements.txt` / `pyproject.toml`) exists — still true, out of scope for a
  cleanup pass (would require pinning versions someone has to verify against production).
- Base DB schema DDL (`du_lieu_production_schema_v2.sql`, `hh_control_tower_db_upgrade_v2.sql`) is not
  tracked in this repo — still true, out of scope.
- `BUNDLE_MANIFEST.json` was already updated in the working tree (uncommitted, predates this pass) to
  correctly describe the v4.7.4 release and its config CSVs — just needs a commit, not a rewrite.
- Three independent P&L implementations (channel ETL, `control_tower`/`mart` SQL views, `api/routers/*.py`)
  are not guaranteed to agree — documented in `CLAUDE.md`'s "Known architectural risks", unchanged by this
  pass.
