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

## 2. Versioned PowerShell — classification (per user instruction: not deleted this round)

None of these were deleted, because Windows Task Scheduler configuration is not visible from this repo and
a versioned `.ps1` could still be triggered by scheduled-task muscle memory outside the repo.

| File | Class | Notes |
|---|---|---|
| `run_daily_all.ps1` | KEEP_CURRENT | Authoritative daily orchestrator (CLAUDE.md) |
| `run_daily_pnl_v4_7_4.ps1` | KEEP_CURRENT | Current CEO Daily P&L orchestrator |
| `install_ceo_daily_pnl_v4_7_4.ps1` | KEEP_CURRENT | Current installer/self-test |
| `run_preview_email.ps1` | KEEP_CURRENT | Cited in CLAUDE.md security rules |
| `run_send_ceo_daily_individual.ps1` | KEEP_CURRENT | Wraps `send_ceo_daily_excel_individual.py` |
| `run_daily_pnl_v4_6.ps1`, `run_daily_pnl_v4_6_1.ps1`, `run_daily_pnl_v4_7.ps1`, `run_daily_pnl_v4_7_1.ps1`, `run_daily_pnl_v4_7_2.ps1`, `run_daily_pnl_v4_7_3.ps1` | LEGACY_CANDIDATE | No in-repo caller; each targets a builder/validator step that is still load-bearing as a *dependency* of v4.7.4, but the wrapper script itself looks superseded by `run_daily_pnl_v4_7_4.ps1` |
| `run_daily_pnl_v4.ps1` | LEGACY_CANDIDATE (paired) | Only caller of `scripts/build_ceo_daily_pnl_package_v4_0.py`; kept together — deleting one without the other would leave a dangling reference |
| `install_ceo_daily_pnl_v4_6.ps1`, `_v4_7.ps1`, `_v4_7_1.ps1`, `_v4_7_2.ps1` | LEGACY_CANDIDATE | Superseded by `install_ceo_daily_pnl_v4_7_4.ps1`; still the only callers of `tools/apply_ceo_daily_pnl_v4_6_upgrade.py` / `v4_6_1_upgrade.py`, so kept together |
| `run_daily_inventory_and_ceo_v4_7.ps1`, `_v4_7_1.ps1`, `_v4_7_2.ps1`, `_v4_7_3.ps1` | UNKNOWN_EXTERNAL_DEPENDENCY | See gap #3 above — do not delete before confirming the current inventory-population path |

**Recommendation for a future pass**: once ops confirms none of these are still triggered by Task
Scheduler, delete the LEGACY_CANDIDATE rows above and their now-orphaned Python targets
(`scripts/build_ceo_daily_pnl_package_v4_0.py`, `tools/apply_ceo_daily_pnl_v4_6_upgrade.py`,
`tools/apply_ceo_daily_pnl_v4_6_1_upgrade.py`) together, then consolidate what's left into the two files
CLAUDE.md already recommends: `scripts/run_daily.ps1` and `scripts/bootstrap.ps1`.

## 3. One-shot patchers kept only because CLAUDE.md cites them by name

`tools/apply_tiktok_v4_12_brand_group_from_filename.py`, `tools/apply_tiktok_v4_14_partner_commission.py`
— both already applied (target code confirmed present in `scripts/tiktok_etl.py`), zero other references,
but `CLAUDE.md` explicitly names them in a "do not rerun" warning paragraph. Deleting the files without
first editing that paragraph would leave a dangling doc reference. Recommendation: in a documentation-focused
pass (not this one), move both to `docs/archive/`, then update the `CLAUDE.md` paragraph to point at the
new path (or drop the file-existence claim and keep only the "already applied, do not rerun logically
equivalent patch" warning).

`tools/apply_ceo_daily_pnl_v4_6_upgrade.py`, `tools/apply_ceo_daily_pnl_v4_6_1_upgrade.py` — already
applied (their markers are the literal text now living in `CLAUDE.md`'s own "v4.6 status contract" /
"v4.6.1 cleanup contract" sections), but still actively called by the kept `install_ceo_daily_pnl_v4_6.ps1`
/ `_v4_7.ps1` installers (§2). Retire together with those installers.

`tools/apply_marketplace_exact_cancel_patch_v4_5.py`, `tools/apply_shopee_gross_all_status_patch_v4_1.py`
— **not a cleanup candidate at all right now**: see gap #2 above. Keep until the working-tree ETL changes
they produced are committed.

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
