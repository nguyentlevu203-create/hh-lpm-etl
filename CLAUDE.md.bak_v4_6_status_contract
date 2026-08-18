# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
- **FastAPI entry point:** `main.py` → `api/main.py` (uvicorn, `0.0.0.0:8000`).

Neither the CEO Control Tower package/report nor the weekly/monthly package/report is wired into
`run_daily_all.ps1` — both are run manually/out-of-band. Only the 4 ETLs + staff scorecard email are
scheduled daily.

## Current vs legacy

**CEO Control Tower package builder chain** (all in `scripts/`):
`build_ceo_control_tower_package.py` (v1, dead) →
`build_ceo_control_tower_package_v2_scorecard.py` (legacy standalone, but **still imported as a dependency
module** by v2_10 via `importlib` — do not delete) →
`build_ceo_control_tower_package_v2_8_samples.py` (same status: legacy standalone, load-bearing importlib
dependency of v2_10) →
`build_ceo_control_tower_package_v2_9_ceo_email_scorecard.py` (fully superseded, dead) →
**`build_ceo_control_tower_package_v2_10_ceo_email_scorecard_samples_compatible.py` = CURRENT**, confirmed
by `outbox/packages/ceo_control_tower_package_v2_10_*` being the only daily package output present.

**Weekly/monthly sender:** `scripts/send_ceo_growth_week_month_excel_report.py` = current;
`scripts/send_ceo_growth_week_month_report.py` = legacy near-duplicate (6-line cosmetic diff only).

**Orphaned dead code:** `scripts/send_ceo_growth_report_gmail.py` — the only sender using Google OAuth
(`InstalledAppFlow`) instead of SMTP+app-password; nothing references it, and no `credentials.json`/
`token.json` exists in the repo to even run it.

> **Do not edit v1, v2_9, the non-excel week/month sender, or the gmail-oauth sender unless the user
> explicitly asks to work on legacy code.** Default to the CURRENT files listed above.

**TikTok patch scripts are historical, one-shot, already-applied:** `tools/apply_tiktok_v4_12_brand_group_from_filename.py`
and `tools/apply_tiktok_v4_14_partner_commission.py` are regex-based source patchers that already rewrote
`scripts/tiktok_etl.py` in place (their target code is already present in the current file). **Do not
rerun them** — they match exact prior source text; rerunning against already-patched code will either
raise `RuntimeError` (safe no-op) or, if surrounding code has since changed, corrupt the file.

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
- **Environment-variable inconsistency**: DB config is defined twice, each with its own hardcoded fallback
  default — `etl_common.py:28-34` (ETL/write path) and `api/database.py:5-11` (API/read path). There is no
  single source of truth; changing DB config requires updating both.

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
- **Windows machine coupling**: `run_daily_all.ps1:3` hardcodes
  `cd C:\Users\ADMIN\Downloads\ETL_production_v3_exact_codes`, which does not match this repo's checkout
  path; `scripts/shopee_etl.py:757-786` has Windows-file-lock archive-retry logic, confirming production
  runs on Windows.
- **Package-builder version drift**: multiple versioned copies of the same builder coexist (see Current vs
  legacy). Always verify current-production status against `outbox/` output evidence, not filename version
  numbers alone, before editing.
- **No `.gitignore`**: `processed/`, `outbox/`, most of `data/`, and `__pycache__/*.pyc` are all tracked in
  git, and a GitHub `origin` remote is configured. Do not reflexively `git add -A`/`git add .`.

## Security rules

- Never expose, print, or repeat any committed secret value (DB password, SMTP password, API keys) found
  anywhere in this repo, including in `run_daily_all.ps1` / `run_preview_email.ps1` and the hardcoded
  fallback defaults in `etl_common.py` / `api/database.py`. Reference them by file/line only.
- Never add new credentials to source files. Use environment variables; do not hardcode secrets as a
  "temporary" measure.
- Never commit a `.env` file or any file containing credentials.
- Never expose production DB credentials in logs, error messages, commit messages, or chat output.
- Never send emails (real or dry-run) unless the user explicitly instructs it for that turn.
- Never connect to or mutate the production database while doing analysis/audit work — read schema/data
  only when explicitly asked to run something, and prefer reading source code over querying live data.
- Never run destructive SQL (`DROP`, `TRUNCATE`, `DELETE` without a scoped `WHERE`, etc.) without explicit
  user authorization for that specific statement.

## Safe workflow

Before making any modification:

1. Read this `CLAUDE.md`.
2. Run `git status`.
3. Identify the current production implementation (see "Current vs legacy") — don't edit a legacy/dead
   file by mistake.
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
