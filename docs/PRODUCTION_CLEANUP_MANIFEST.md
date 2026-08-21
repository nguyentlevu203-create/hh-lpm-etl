# Production Cleanup Manifest — 2026-08-21 pass

- Repo: `nguyentlevu203-create/hh-lpm-etl`
- HEAD at start of this pass: `ee5d4e7` (branch `main`)
- Base evidence: `docs/PRODUCTION_CLEANUP_INVENTORY.md` (full per-file classification, written 2026-08-20
  against HEAD `14db86a`; re-verified below against the current HEAD/working tree — nothing in the two
  commits between `14db86a` and `ee5d4e7` touched any of the files decided below).
- Methodology: `grep -rn` reference scan across `*.py *.ps1 *.sql *.md *.txt *.csv *.json` for every DELETE
  candidate, plus reading each candidate file's own content to confirm it is genuinely superseded/applied
  before deleting. No file was deleted without a zero-reference grep result shown below.
- Scope: this table lists only files with an actual **DELETE / UNTRACK / MOVE / REVIEW** call in this pass.
  Everything else (roughly 125 tracked files) is **KEEP** — see `docs/PRODUCTION_CLEANUP_INVENTORY.md` for
  the full KEEP evidence per file; it is not repeated here to avoid a redundant 130-row wall.

## Executed this pass

| Path | Category | Reference Found | Decision | Reason |
|---|---|---|---|---|
| `scripts/build_ceo_daily_pnl_package_v4_1.py` | legacy builder | no (only prior audit doc + own docstring) | DELETE | Superseded by v4.5 chain root; `build_ceo_daily_pnl_package_v4_5.py`'s importlib chain never loads it; zero callers |
| `scripts/validate_ceo_daily_pnl_package_v4_5.py` | legacy validator | no | DELETE | Validator chain root is `validate_ceo_daily_pnl_package_v4_6.py` (confirmed no `BASE_FILENAME`/importlib link back to v4_5); zero callers |
| `apply_ceo_discount_etl_upgrade.py` (root) | one-shot patcher | no | DELETE | Zero references anywhere; patch confirmed **already fully applied** — `total_subsidy_amount`/`shop_voucher_amount` present in `scripts/shopee_etl.py` (7 matches), `seller_discount_amount` aggregation/EXCLUDED-column present in `scripts/tiktok_etl.py` (2 matches) |
| `README_v4_6.md` | legacy doc | yes (own filename only) | DELETE | Superseded release note; content merged into `CHANGELOG.md` |
| `README_v4_7.md` | legacy doc | yes (own filename only) | DELETE | Superseded release note; content merged into `CHANGELOG.md` |
| `README_v4_7_1.md` | legacy doc | yes (own filename only) | DELETE | Superseded release note; content merged into `CHANGELOG.md` |
| `README_v4_7_2.md` | legacy doc | yes (own filename only) | DELETE | Superseded release note; content merged into `CHANGELOG.md` |
| `README_v4_7_3.md` | legacy doc | yes (own filename only) | DELETE | Superseded release note; content merged into `CHANGELOG.md` |
| `.gitignore` | config | n/a | MOVE→edit | Extended with `.pytest_cache/`, `.ruff_cache/`, `.coverage`, `venv/`, `.vscode/`, `.idea/`, `.DS_Store`, `Thumbs.db`, `*.log`, `*.tmp`, `~$*.xlsx`, `qa/generated/` (none of these previously covered; nothing currently tracked matches any of them, confirmed before adding) |
| `CHANGELOG.md` | new doc | n/a | CREATE | New file — consolidates the 5 deleted `README_v4_*.md` plus `README.txt` (v4.13 patch) and the 2026-08-20 cleanup commit, per required deliverable |
| `REFACTOR_LATER.md` | new doc | n/a | CREATE | New file — required deliverable for CEO Daily chain / Control Tower chain / versioned-PS1 classification / pending patchers, none of which were touched this pass |
| `docs/PRODUCTION_CLEANUP_MANIFEST.md` | new doc | n/a | CREATE | This file |

## Deliberately NOT executed despite looking deletable (safety overrides)

| Path | Category | Reference Found | Decision | Reason |
|---|---|---|---|---|
| `README.txt` | legacy doc | yes — `CLAUDE.md` cites it by name ("not documented in `README.txt`, which only describes the older v4.13 patch") | KEEP | Deleting it would leave a dangling citation in `CLAUDE.md`. Content also merged into `CHANGELOG.md` for convenience, but the file itself stays |
| `scripts/build_ceo_daily_pnl_package_v4_0.py` | legacy builder | yes — only caller is `run_daily_pnl_v4.ps1` | KEEP (REFACTOR_LATER) | Its only caller is a versioned `.ps1`, and per explicit instruction this round does not delete versioned PowerShell without confirming Task Scheduler status. Deleting the Python file while keeping the `.ps1` would silently break that script if it is ever run |
| `run_daily_pnl_v4.ps1` | legacy orchestrator | no external caller found | KEEP (REFACTOR_LATER) | Versioned PowerShell — not deleted this round per explicit instruction; see `REFACTOR_LATER.md` §2 for the full classification table |
| `run_daily_pnl_v4_6.ps1` … `_v4_7_3.ps1`, `install_ceo_daily_pnl_v4_6.ps1` … `_v4_7_2.ps1`, `run_daily_inventory_and_ceo_v4_7.ps1` … `_v4_7_3.ps1` | legacy orchestrators/installers | no external caller found | KEEP (REFACTOR_LATER) | Same reason; `run_daily_inventory_and_ceo_v4_7*.ps1` additionally protected by the inventory-orchestration gap (`REFACTOR_LATER.md` §1.3) — there is no v4.7.4 successor, so deleting these before confirming the current inventory-population path risks breaking inventory data entirely |
| `tools/apply_tiktok_v4_12_brand_group_from_filename.py`, `tools/apply_tiktok_v4_14_partner_commission.py` | applied one-shot patcher | yes — `CLAUDE.md` cites both by name in a "do not rerun" warning | KEEP | Deleting would leave a dangling `CLAUDE.md` citation; already applied, zero other references — safe candidate for a documentation-paired future pass (`REFACTOR_LATER.md` §3) |
| `tools/apply_ceo_daily_pnl_v4_6_upgrade.py`, `tools/apply_ceo_daily_pnl_v4_6_1_upgrade.py` | applied one-shot patcher | yes — called by kept `install_ceo_daily_pnl_v4_6.ps1` / `_v4_7.ps1` | KEEP | Still load-bearing for the (kept) legacy installers above |
| `tools/apply_marketplace_exact_cancel_patch_v4_5.py`, `tools/apply_shopee_gross_all_status_patch_v4_1.py` | pending/uncommitted patch source | yes — `CLAUDE.md` cites both by name as the source of the current uncommitted ETL working-tree state | KEEP | Not a legacy patcher at all right now — it's the only record of how to reproduce today's uncommitted `shopee_etl.py`/`nhanh_etl.py`/`tiktok_etl.py` state. See `REFACTOR_LATER.md` §1.2 |
| `scripts/apply_shopee_gift_patch_v4_7_4.py`, `scripts/shopee_gift_classifier_v4_7_4.py` | pending patcher / its target | confirmed NOT applied (precondition blob mismatch) | KEEP | Explicitly protected by `CLAUDE.md` — pending, not legacy |
| `scripts/build_ceo_daily_cumulative_package.py` | unclear purpose | zero references | REVIEW | Could be an abandoned experiment or an undocumented manual report; needs a human answer, not a grep-based deletion. No action taken |
| `scripts/import_sample_orders_v2.py`, `data/sample_orders/2026-06/TIKTOK/*.xlsx` | unclear purpose | zero external references | REVIEW | Same reasoning; writes to `control_tower.sample_orders`, unclear if still used |
| `BUNDLE_MANIFEST.json` | manifest | n/a | KEEP as-is | Already updated in the working tree (uncommitted, predates this pass) to correctly list the v4.7.4 release, base, and all 4 config CSVs — no further edit needed, just eventually needs a commit |
| `import_costs.py` | script | `DEFAULT_FILE` still points at `"Giá nhập 25.06.2026.xlsx"` | KEEP as-is | That file is present and tracked in the repo (with pending uncommitted data edits) — the default path is still valid, no dangling reference to fix |
| `sql/audit_*.sql` (7 files) | manual audit queries | mixed (some zero-ref) | KEEP | Same category as CLAUDE.md-protected `sql/checks/check_shopee_scorecard_v2_7.sql`; DB-history/audit value, not confirmed dead by absence of grep hits alone |
| `data/mt_gt/2_master_data/*`, `data/tiktok/8_master_data/*`, `data/inventory/rules/*`, `"Giá nhập 25.06.2026.xlsx"` | config source of truth | yes — read directly by the channel ETLs / `import_costs.py` | KEEP tracked | Actively read by current production ETL code; some have pending uncommitted data edits (leave those edits untouched — out of scope) |

## Already executed in a prior pass (2026-08-20, not repeated here)

Self-test/example/checksum artifacts, `docs/email_drafts/*.eml`, and 91 `data/inventory/daily/**` files
were already deleted/untracked in commit `31102ac`; secrets were already moved to environment variables in
commit `ee5d4e7`. See `CHANGELOG.md`'s "2026-08-20" entry. Verified via `git diff --stat 14db86a..HEAD`
that neither commit touched any file decided in this pass.
