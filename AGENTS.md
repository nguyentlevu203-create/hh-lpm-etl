# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project identity

`hh-lpm-etl` is the production ETL + BI reporting system for a Vietnamese multi-channel FMCG
distributor (brand "LPM" / Le Petit Marseillais Vietnam, plus a "Sữa chua / Blédina" sub-brand). Its
business purpose is to ingest daily sales/ads/settlement exports from every sales channel, compute
per-channel P&L (revenue, COGS, platform fees, ads, backoffice, packaging, returns, profit), and deliver
that as CEO and staff reporting.

Main channels: **Nhanh.vn** (own e-commerce/CRM), **Shopee**, **TikTok Shop**, **MT** (Modern Trade) and
**GT** (General Trade) — MT and GT share a single ETL script (`scripts/mt_gt_etl.py`), distinguished by
`channel_group`.

Central data warehouse: PostgreSQL database **`DuLieu`** (default `localhost:5433`, user `postgres`).
There are two independent DB-connection code paths that must be kept in sync manually — `etl_common.py`
(ETL/write path) and `api/database.py` (API/read path).

## Production architecture

```
RAW DATA (data/<channel>/... Excel/CSV)
  → ETL (scripts/<channel>_etl.py, via etl_common.py)
  → PostgreSQL DuLieu (nhanh.*, shopee.*, tiktok.*, mtgt.*, dim.*, etl.*, inventory.*)
  → reporting SQL/views (control_tower.*, mart.*)
  → three consumer layers:
      - API (api/main.py + api/routers/*.py)
      - CEO packages (scripts/build_ceo_*.py → outbox/packages/*.json + .md)
      - staff/CEO email reporting (scripts/send_*.py → Gmail SMTP → outbox/reports/, outbox/email_drafts/)
```

Channel ETL scripts are siloed by design — each writes only to its own schema (see module docstrings,
e.g. "Writes only to shopee.* tables"). Cross-channel aggregation happens later, in the SQL view layer and
in `api/routers/ceo.py`, never inside a channel ETL script.

## Current production entry points

- **`run_daily_all.ps1`** — the daily production orchestrator (Windows/PowerShell): DB check → 4 channel
  ETLs → `send_staff_scorecard_emails.py`. This is the authoritative definition of "daily production."
- **`scripts/nhanh_etl.py`**, **`scripts/shopee_etl.py`**, **`scripts/tiktok_etl.py`**,
  **`scripts/mt_gt_etl.py`** — the 4 channel ETL entry points, all invoked by `run_daily_all.ps1`.
- **`scripts/send_staff_scorecard_emails.py`** — current daily staff/CEO scorecard email sender.
- **CEO Control Tower package builder (current):**
  `scripts/build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible.py`.
- **Weekly/monthly CEO Growth package builder (current):**
  `scripts/build_ceo_growth_package_v3_3_week_month.py`, emailed via
  `scripts/send_ceo_growth_week_month_excel_report.py`.
- **CEO Daily P&L package builder (current):** `scripts/build_ceo_daily_pnl_package_v4_7_4.py`,
  validated by `scripts/validate_ceo_daily_pnl_package_v4_7_4.py`, run via `run_daily_pnl_v4_7_4.ps1`,
  contract documented in `docs/CEO_DAILY_PNL_V4_7_4_CONTRACT.md`. Adds product mapping (short-SKU/EAN
  alias identity), Shopee gift classification, and inventory push visibility on top of the v4.7.3 (HF1)
  layer, which itself is additive on the validated v4.6.1 financial/status contract (unchanged waterfall,
  no EBITDA) — see "Current vs legacy" below for the full chain. Sent individually per recipient via
  `scripts/send_ceo_daily_excel_individual.py` (see `config/ceo_daily_email_recipients.csv`).
  ⚠️ **v4.7.4 gift-classification prerequisite not yet applied**: `scripts/apply_shopee_gift_patch_v4_7_4.py`
  has not been run against `scripts/shopee_etl.py` (confirmed by `docs/PRODUCTION_CLEANUP_INVENTORY.md` —
  the patch's precondition text is absent from both HEAD and the working tree, and
  `scripts/shopee_gift_classifier_v4_7_4.py` is not imported anywhere). Until that patch is applied,
  v4.7.4's Shopee gift fields will not reflect real gift classification. Do not treat the patcher as
  "already applied, safe legacy."
- **FastAPI entry point:** `main.py` → `api/main.py` (uvicorn, `0.0.0.0:8000`).

Neither the CEO Control Tower package/report nor the weekly/monthly package/report is wired into
`run_daily_all.ps1` — both are run manually/out-of-band. Only the 4 ETLs + staff scorecard email are
scheduled daily.

## Current vs legacy

**CEO Control Tower package builder chain** (all in `scripts/`):
`build_ceo_control_tower_package_v2_scorecard.py` (legacy standalone, but **still imported as a dependency
module** by v2_10 via `importlib` — do not delete) →
`build_ceo_control_tower_package_v2_8_samples.py` (same status: legacy standalone, load-bearing importlib
dependency of v2_10) →
**`build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible.py` = CURRENT**, confirmed
by `outbox/packages/ceo_control_tower_package_v2_10_*` being the only daily package output present.
(v1 `build_ceo_control_tower_package.py` and v2_9 `..._v2_9_ceo_email_scorecard.py` were fully superseded,
unreferenced anywhere else, and deleted during the 2026-08-19 repo cleanup — restorable from git history
if ever needed.)

**Weekly/monthly sender:** `scripts/send_ceo_growth_week_month_excel_report.py` = current. (The legacy
non-Excel near-duplicate `send_ceo_growth_week_month_report.py` and the orphaned Google-OAuth sender
`send_ceo_growth_report_gmail.py` — unreferenced anywhere, no `credentials.json`/`token.json` in the repo —
were both deleted during the 2026-08-19 repo cleanup.)

**CEO Daily P&L builder chain** (all in `scripts/`): `build_ceo_daily_pnl_package_v4_5.py` (chain root;
earlier `v4_0`/`v4_1` are confirmed dead per `docs/PRODUCTION_CLEANUP_INVENTORY.md` — not loaded by
anything current) → `v4_6` → `v4_6_1` (financial/status contract baseline, still the authoritative source
the v4.7.x layer rebuilds prior periods from) → `v4_7` → `v4_7_1` → `v4_7_2` → `v4_7_3` (HF1) →
**`v4_7_4` = CURRENT**, confirmed by `BUNDLE_MANIFEST.json`'s `"release": "v4.7.4"`. The builder/validator
chain is an `importlib`-loaded cascade — `v4_7_4` loads `v4_7_3` loads `v4_7_2` ... down to `v4_5`
(builder) / `v4_6` (validator, no earlier link) — every intermediate file is load-bearing; do not delete
any link without first flattening the chain (see `docs/PRODUCTION_CLEANUP_INVENTORY.md`'s high-risk-phase
notes). Each `v4_7*` step is purely additive on top of v4.6.1's P&L waterfall — see
`docs/CEO_DAILY_PNL_V4_7_4_CONTRACT.md` and the sibling `docs/CEO_DAILY_PNL_V4_7_*_CONTRACT.md` files for
what each step added. Do not skip straight to editing v4.6/v4.6.1 business logic without checking whether
the same concept was layered again in v4.7.x/v4.7.4.

⚠️ `scripts/shopee_etl.py`, `nhanh_etl.py`, `tiktok_etl.py` currently have **uncommitted working-tree
changes** (exact-cancel/gross-all-status logic from `tools/apply_marketplace_exact_cancel_patch_v4_5.py` /
`tools/apply_shopee_gross_all_status_patch_v4_1.py`) not yet in the `origin/main` HEAD blob. A fresh clone
today would run the *older* committed behavior, not what's on this machine. Resolve (commit the
working-tree state, after review) before relying on "fresh clone = current production."

> **Do not edit dead/legacy files listed above unless the user explicitly asks to work on legacy code.**
> Default to the CURRENT files listed above.

**Two TikTok rules were originally delivered as one-shot source patches, now fully merged into
`scripts/tiktok_etl.py`** — brand_group follows filename-derived shop_label, and partner affiliate
commission parsing lives in `add_partner_commission()`. Both rules are permanent parts of the current file;
no separate patcher script needs to exist in the repo for them to remain in effect. See `CHANGELOG.md` for
the historical patch record (the one-shot patcher scripts themselves were removed once confirmed fully
merged and zero-referenced elsewhere).

## Critical business rules

Reference only — read the cited lines before touching related logic, do not "simplify" them:

- **Nhanh net revenue**: `order_value` from the exact header `"Giá trị đơn hàng"` only, validated on the
  original order-header row before forward-fill — `scripts/nhanh_etl.py:117,186-233`.
- **Nhanh shipping**: `Phí vận chuyển − Phí ship báo khách + 25,000 VND` if status is exactly `"Đã hoàn"`
  — `scripts/nhanh_etl.py:234-240`, mirrored in
  `sql/migrations/hh_control_tower_db_upgrade_v2_12_nhanh_shipping_bo_return_rules.sql:11-14`.
- **Shopee gift exclusion**: gift/giveaway rows (price ≤ 0 or keyword match) are excluded from
  `gross_sales` but kept in the legacy `seller_revenue`/`est_payout` calculation — `scripts/shopee_etl.py:171-190,378-393`.
- **Shopee combo COGS**: `A+B+C` SKUs sum per-component COGS; any missing component **aborts the whole
  load** rather than defaulting to 0 — `scripts/shopee_etl.py:90-107,585-627`.
- **TikTok fee logic**: platform fee rate depends on brand AND an exact date cutoff (2026-05-08) —
  `scripts/tiktok_etl.py:78-107`.
- **TikTok Voucher Extra Program (VXP) fee**: base service fee is **5%** of `fee_base_item`, brand-agnostic
  — `scripts/tiktok_etl.py:1052` (`f_vxp = round(fee_base_item * 0.05, 0)`). Source: TikTok Shop Vietnam
  policy, current 2026 policy, confirmed by business owner 2026-08-21. Policy also states a **maximum of
  50,000 VND per product**, but **that cap is not implemented in code today** — `fee_base_item` is summed
  to the *order* level before this calculation runs (`scripts/tiktok_etl.py:683,691`), so there is currently
  no per-product granularity to cap against. VXP is zeroed only when `is_cancelled` is true (exact
  normalized `"canceled"`); there is no separate "successfully completed" eligibility gate, and no distinct
  handling for a post-completion "Returned"/"Refunded" status if TikTok exports use one. The VXP base
  (`fee_base_item = price*qty - disc_num`) also inherits whatever risk exists in resolving the discount
  column that produces `disc_num` — a pending, not-yet-committed change narrows that column resolution from
  a fuzzy keyword match to an exact `"SKU Seller Discount"` header, which reduces (does not eliminate) the
  risk of a platform-funded discount incorrectly shrinking the VXP base; not yet in this repo's committed
  code. Do not treat the 50,000 VND cap as enforced; do not add "completed-only" eligibility without an
  explicit owner decision.
- **TikTok booking rule (v4.21.1)**: booking fee applies only when exactly one creator maps via the exact
  header `"Tên người dùng nhà sáng tạo"` AND content type is exactly `"Phát trực tiếp"`; 0 or >1 mapped
  creators → booking = 0 — `scripts/tiktok_etl.py:678-691,832-846`.
- **TikTok ads VAT**: ads cost is stored **pre-VAT** in the DB; a generated column `cost_vnd_vat` applies
  10% VAT downstream — never add VAT again in application code — `scripts/tiktok_etl.py:1063-1065`.
- **MT/GT gross revenue**: `"Doanh số bán" × 1.08` (adds 8% VAT in code) — `scripts/mt_gt_etl.py:392-411`
  (v4.23 rule; **not** documented in `README.txt`, which only describes the older v4.13 patch).
- **MT/GT net revenue**: taken verbatim from `"Tổng thanh toán"`, never recomputed from qty × price —
  `scripts/mt_gt_etl.py:392-411`.
- **MT/GT CVC** (shipping) lines: detected by product code `"CVC"` or name containing "chi phí vận
  chuyển"; `cvc_amount = abs(total_payment)`, zero revenue/COGS — `scripts/mt_gt_etl.py:287-290,399-406`.
- **Packaging cost**: flat 2,000 VND × quantity, reimplemented independently per channel —
  `api/routers/nhanh.py:28`, `api/routers/shopee.py:37`, `scripts/tiktok_etl.py:1022-1029`.
- **Shared product cost source**: `dim.product_costs` is the single COGS source for **all** channels, via
  `etl_common.load_cost_map()`/`lookup_unit_cost()` (`etl_common.py:578-597`), populated by
  `import_costs.py` from `Giá nhập 25.06.2026.xlsx`.
- **Booking policy v6** (staff email rule): MT/GT/DIGITAL/SHOPEE booking is always 0; TikTok booking uses
  only `tiktok.orders_pnl.booking_fee` — documented in `scripts/send_staff_scorecard_emails.py:1-16`.
- **Generic profit formula**: `profit = net_revenue − cogs − shipping/return_fee − platform_fees −
  backoffice_fee − packaging_cost − ads_cost` (plus `− live_cost − booking_fee` for TikTok). Same shape
  reimplemented independently in `api/routers/{nhanh,shopee,tiktok}.py` and in
  `sql/migrations/...v2_12...sql:85-100` — see Known architectural risks for where this diverges.

## Database conventions

- DB name `DuLieu`, default `localhost:5433`, user `postgres`.
- Schemas: `nhanh`, `shopee`, `tiktok`, `mtgt` (channel-siloed write targets); `dim` (shared product
  cost/master); `etl` (batch/error audit); `inventory` (stock snapshots/reorder rules); `control_tower` /
  `mart` (reporting views); implicit `util` schema (`util.norm_code`).
- **`dim.product_costs`** — shared COGS table, loaded by `import_costs.py`, read by all 4 channel ETLs.
- **ETL audit tables**: `etl.batch_runs` (one row per import run, via `begin_batch()`/`finish_batch()`) and
  `etl.import_errors` (row-level failures with raw JSON payload via `log_import_error()`) — both defined
  in `etl_common.py`.
- **`control_tower.*` / `mart.*`** — reporting view layer on top of the raw schemas, built incrementally
  by `hh_growth_week_month_views_v3_0.sql` and `sql/migrations/*.sql`. The *base* schema/views these
  depend on are not tracked in this repo (see Known architectural risks).
- **Environment-variable inconsistency**: DB config is defined twice — `etl_common.py:28-38` (ETL/write
  path) and `api/database.py:5-19` (API/read path). Non-secret values (`dbname`/`user`/`host`/`port`) each
  keep their own hardcoded fallback default; `password` has no fallback in either (see Security rules) and
  raises if `ETL_DB_PASSWORD` is unset. There is no single source of truth; changing DB config requires
  updating both. See `.env.example` for the full set of variables either path can read.

## Reporting architecture

- **A. Production daily staff scorecard** — `scripts/send_staff_scorecard_emails.py`. Recipient-scoped by
  `can_view_profit`/`recipient_type`, `DRY_RUN`-aware, wired into `run_daily_all.ps1`.
- **B. CEO Control Tower package (daily)** — built by
  `scripts/build_ceo_control_tower_package_v2_10_...py` into `outbox/packages/ceo_control_tower_package_v2_10_*`,
  emailed by `scripts/send_ceo_control_tower_report.py`. Not part of `run_daily_all.ps1`.
- **C. Weekly/monthly CEO Growth package** — built by `scripts/build_ceo_growth_package_v3_3_week_month.py`
  into `outbox/packages/ceo_growth_package_v3_3_week_month_*`, emailed by
  `scripts/send_ceo_growth_week_month_excel_report.py`. Not part of `run_daily_all.ps1`.

## Known architectural risks

- **Duplicated P&L logic**: three independent implementations of the same numbers — channel ETL (write
  path), `control_tower`/`mart` SQL views (used by report builders), and `api/routers/*.py`'s own
  hand-written SQL. Not guaranteed to agree. Concretely verified: `api/routers/mt_gt.py:40-41` uses a flat
  2%/15% shipping/backoffice model while `scripts/mt_gt_etl.py` captures shipping via real per-document
  CVC lines — the API's MT/GT profit and the CEO email/package's MT/GT profit are genuinely different
  numbers today.
- **Missing base schema DDL**: `du_lieu_production_schema_v2.sql` and `hh_control_tower_db_upgrade_v2.sql`
  are referenced by name (`etl_common.py`, `sql/seed/seed_product_master_v1.sql`) but not tracked in this
  repo — a fresh DB cannot be bootstrapped from this repo alone.
- **Missing tests**: no unit/integration tests anywhere; the only validation artifact is
  `sql/checks/check_shopee_scorecard_v2_7.sql`, a manual query hardcoded to one date.
- **Missing dependency lock/requirements**: no `requirements.txt`/`pyproject.toml`/lockfile.
  Dependencies (`pandas`, `psycopg2`, `openpyxl`, `fastapi`, `uvicorn`, plus Google API client libs used
  only by the orphaned gmail-oauth sender) are installed ad hoc.
- **Windows machine coupling**: `scripts/shopee_etl.py:757-786` has Windows-file-lock archive-retry logic,
  confirming production runs on Windows. (`run_daily_all.ps1` / `run_preview_email.ps1` no longer hardcode
  a checkout path — as of the 2026-08-20 production-cleanup pass they use `$PSScriptRoot` instead; see
  Security rules for the credential side of that same cleanup.)
- **Package-builder version drift**: multiple versioned copies of the same builder coexist (see Current vs
  legacy). Always verify current-production status against `outbox/` output evidence, not filename version
  numbers alone, before editing.
- **`.gitignore` added 2026-08-19**: `processed/`, `outbox/`, `__pycache__/`, `*.pyc` and `backups/*.dump`
  are now ignored going forward (they were previously tracked in git, generating most of `.git`'s size).
  Still do not reflexively `git add -A`/`git add .` — review `git status` before staging.
- **`data/` tracking narrowed 2026-08-20**: only source-of-truth config/master files stay tracked —
  `data/*/2_master_data/` (mt_gt), `data/tiktok/8_master_data/`, `data/inventory/rules/`, plus
  `"Giá nhập 25.06.2026.xlsx"`. Raw per-channel runtime exports and `data/inventory/daily/` snapshots are
  now gitignored (previously-committed inventory snapshots were `git rm --cached`, kept on disk). See
  `docs/PRODUCTION_CLEANUP_INVENTORY.md` for the full file-by-file classification this was based on.

## Security rules

- As of the 2026-08-20 production-cleanup pass, `run_daily_all.ps1`, `run_preview_email.ps1`,
  `etl_common.py`, and `api/database.py` no longer hardcode any DB/SMTP secret — they read
  `ETL_DB_PASSWORD`/`SMTP_USER`/`SMTP_PASSWORD` etc. from the environment only (see `.env.example` for the
  full variable list) and fail closed with a clear error if unset. **Those same 4 files still carry the
  old hardcoded DB password and Gmail App Password in git history** (multiple prior commits, already
  pushed to `origin/main`) — history was not rewritten (rebase/filter-repo/force-push are out of scope
  without explicit authorization). Treat both secrets as compromised: **manual rotation of the Postgres
  password and the Gmail App Password is still required**, independent of any code cleanup.
- Never expose, print, or repeat any committed secret value (DB password, SMTP password, API keys) found
  anywhere in this repo or its git history. Reference them by file/line (or commit hash) only.
- Never add new credentials to source files. Use environment variables via `.env` (see `.env.example`); do
  not hardcode secrets as a "temporary" measure.
- Never commit a `.env` file or any file containing credentials. (`.env`/`.env.*` are gitignored;
  `.env.example` is the only tracked template, with no real values.)
- Never expose production DB credentials in logs, error messages, commit messages, or chat output.
- Never send emails (real or dry-run) unless the user explicitly instructs it for that turn.
- Never connect to or mutate the production database while doing analysis/audit work — read schema/data
  only when explicitly asked to run something, and prefer reading source code over querying live data.
- Never run destructive SQL (`DROP`, `TRUNCATE`, `DELETE` without a scoped `WHERE`, etc.) without explicit
  user authorization for that specific statement.

## Safe workflow

Before making any modification:

1. Read this `AGENTS.md`.
2. Run `git status`.
3. Identify the current production implementation (see "Current vs legacy") — don't edit a legacy/dead
   file by mistake. For a full file-by-file classification with dependency evidence, see
   `docs/PRODUCTION_CLEANUP_INVENTORY.md`.
4. Trace which business rules are impacted (see "Critical business rules").
5. Produce a plan before editing.
6. Make the smallest safe change that satisfies the request.
7. Run available validation (there is no test suite — validation means running the specific script against
   real data and checking `etl.import_errors`/`etl.batch_runs`, only when explicitly authorized to do so).
8. Review `git diff` before considering the change done.
9. Never `git commit` or `git push` unless the user explicitly requests it in that turn.

## Commands/workflows requiring explicit approval

The following must never be run proactively — only when the user explicitly asks for that specific action
in that turn:

- Production ETL runs (`scripts/*_etl.py`, `run_daily_all.ps1`)
- Email sending (any `scripts/send_*.py`, with `DRY_RUN=0` or otherwise)
- Database writes of any kind
- Schema migrations (`sql/migrations/*.sql` or new ones)
- `DROP` / `TRUNCATE` / `DELETE` statements
- Credential rotation
- Git history rewriting (`rebase -i`, `filter-repo`, force-push, etc.)
- `git push`
- Production deployment

<!-- CEO_DAILY_PNL_V4_6_STATUS_CONTRACT -->
## CEO Daily P&L v4.6 status contract

- Shopee cancelled: only exact normalized `Đã hủy`.
- TikTok cancelled: only exact normalized `Canceled`.
- Nhanh cancelled: exact trimmed `Đã hủy`, `Hệ Thống hủy`, `Khách hủy`, `HVC hủy`.
- Nhanh return statuses `Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn` are NOT cancellation; `Thất bại` is also NOT cancellation.
- Keep Nhanh +25,000 shipping rule only for exact `Đã hoàn`.
- Nhanh parent daily P&L and the seven exact leaf labels are both required in the v4.6 CEO package.
- Do not rerun the v4.5 single-literal exact-cancel patcher against v4.6 files.
<!-- /CEO_DAILY_PNL_V4_6_STATUS_CONTRACT -->

<!-- CEO_DAILY_PNL_V4_6_1_CLEANUP_CONTRACT -->
## CEO Daily P&L v4.6.1 cleanup contract

- Keep v4.6 financial waterfall unchanged: Gross -> Net Sales -> GM1 -> CM1 -> CM2 -> Profit. No EBITDA.
- Shopee cancelled: exact normalized `Đã hủy` only.
- TikTok cancelled: exact normalized `Canceled` only.
- Nhanh cancellation uses Unicode NFC + case-insensitive + trim/collapse whitespace, then exact phrase matching against `Đã hủy`, `Hệ Thống hủy`, `Khách hủy`, `HVC hủy`. No accent folding, fuzzy matching or substring matching.
- Therefore `Hệ Thống hủy`, `Hệ thống hủy`, and whitespace/case variants are equivalent; `Đã huỷ` is not silently converted to `Đã hủy`.
- Nhanh return statuses `Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn` and failed status `Thất bại` are not cancelled. +25,000 shipping remains for normalized-exact `Đã hoàn` only.
- Cancel-like diagnostics must use whole tokens; `Đang chuyển` must never be flagged merely because it contains the substring `huy` across letters.
- On no-sales/no-source rows used in tables/charts, applicable numeric KPIs are 0; true N/A remains null.
- `success_orders` is a cross-channel non-cancelled compatibility field. For Nhanh literal `Thành công`, use `successful_orders_exact`. Never label Nhanh `success_orders` as literal `Đơn thành công`.
- Use v4.6.1 builder/validator for new CEO packages; keep v4.6 only for rollback.
<!-- /CEO_DAILY_PNL_V4_6_1_CLEANUP_CONTRACT -->
