# hh-lpm-etl

Production ETL + BI reporting system for a Vietnamese multi-channel FMCG distributor (brand "LPM" / Le
Petit Marseillais Vietnam, plus a "Sữa chua / Blédina" sub-brand). Ingests daily sales/ads/settlement
exports from every sales channel, computes per-channel P&L, and delivers CEO and staff reporting.

Channels: **Nhanh.vn**, **Shopee**, **TikTok Shop**, **MT** (Modern Trade) and **GT** (General Trade).

> For full architecture, business-rule references, and the current-vs-legacy file map, see
> **[CLAUDE.md](CLAUDE.md)** — that file is the authoritative guide for working on this codebase, kept in
> sync with production. This README only covers getting a checkout running.

## Prerequisites

- Python 3.x (developed against 3.14) with `pandas`, `psycopg2`, `openpyxl`, `fastapi`, `uvicorn`. No
  dependency lock file exists yet (see [Known gaps](#known-gaps)) — install with:
  ```powershell
  pip install pandas psycopg2-binary openpyxl fastapi uvicorn
  ```
- PostgreSQL reachable database named `DuLieu` (default `localhost:5433`), with the `nhanh`, `shopee`,
  `tiktok`, `mtgt`, `dim`, `etl`, `inventory`, `control_tower`/`mart` schemas already provisioned. This
  repo does not currently contain the base schema DDL to bootstrap a fresh database (see CLAUDE.md's
  "Known architectural risks") — get the base schema from wherever your production DB was provisioned.
- Windows/PowerShell for the `.ps1` production entry points; the channel ETL `.py` scripts and the API are
  platform-agnostic.

## Setup

1. Clone the repo and install the Python dependencies above.
2. Copy `.env.example` to `.env` and fill in real values (DB password, Gmail SMTP user + App Password).
   **Never commit `.env`.**
   ```powershell
   Copy-Item .env.example .env
   ```
3. Load `.env` into your shell session before running anything (PowerShell has no built-in `.env` loader;
   either set each `$env:NAME` manually from your `.env`, or use a small loader snippet/module of your
   choice). `etl_common.py` and `api/database.py` both fail fast with a clear error if `ETL_DB_PASSWORD`
   isn't set — that's the quickest way to confirm your environment is wired correctly.
4. Verify DB connectivity (read-only, no writes):
   ```powershell
   python -c "import os, psycopg2; conn=psycopg2.connect(dbname=os.getenv('ETL_DB_NAME','DuLieu'), user=os.getenv('ETL_DB_USER','postgres'), password=os.getenv('ETL_DB_PASSWORD'), host=os.getenv('ETL_DB_HOST','localhost'), port=os.getenv('ETL_DB_PORT','5433')); print('DB OK'); conn.close()"
   ```

## Running production

- **Daily automated pipeline**: `run_daily_all.ps1` — DB check → 4 channel ETLs
  (`scripts/nhanh_etl.py`, `shopee_etl.py`, `tiktok_etl.py`, `mt_gt_etl.py`) → `send_staff_scorecard_emails.py`.
  This is the only scheduled/automated entry point.
- **CEO Daily P&L (manual, current = v4.7.4)**: `run_daily_pnl_v4_7_4.ps1`, contract in
  `docs/CEO_DAILY_PNL_V4_7_4_CONTRACT.md`.
- **CEO Control Tower / weekly-monthly Growth packages**: manual/out-of-band builders — see CLAUDE.md's
  "Reporting architecture" section for the current entry points and how they differ from the daily
  pipeline.
- **API**: `python main.py` (or `uvicorn main:app`), serves on `0.0.0.0:8000`.

None of the email-sending or database-writing entry points above should be run against production data
without first confirming with whoever owns that data — several are one-way (sends real email, writes
real DB rows).

## Where things live

- **[CLAUDE.md](CLAUDE.md)** — architecture, critical business rules (with exact file:line references),
  current-vs-legacy file map, security rules, known architectural risks.
- **[docs/PRODUCTION_CLEANUP_INVENTORY.md](docs/PRODUCTION_CLEANUP_INVENTORY.md)** — every tracked file
  classified (production runtime / load-bearing dependency / config source of truth / migration / legacy /
  etc.) with the grep/importlib evidence behind each classification.
- **`docs/CEO_DAILY_PNL_V4_7_*_CONTRACT.md`** — one contract doc per CEO Daily P&L release step.
- **`docs/CEO_DAILY_GMAIL_INDIVIDUAL_SENDER.md`** — the per-recipient CEO Daily Excel email sender bundle
  (recipient CSV format, dry-run/validate steps, anti-duplicate logic).
- **`README_v4_7_4.md`**, **`README_HF2.txt`**, **`BUNDLE_MANIFEST.json`**, **`SHA256SUMS_v4_7_4.txt`** —
  kept at repo root (not under `docs/`) because their paths are pinned inside `BUNDLE_MANIFEST.json`'s file
  list and `SHA256SUMS_v4_7_4.txt`'s checksums; the v4.7.4 release install/apply steps and integrity
  record.

## Known gaps

- No automated test suite (one manual SQL check: `sql/checks/check_shopee_scorecard_v2_7.sql`).
- No dependency lock file (`requirements.txt`/`pyproject.toml`).
- Base DB schema DDL isn't tracked in this repo.
- Three independent P&L implementations (channel ETL, SQL views, API routers) aren't guaranteed to agree —
  see CLAUDE.md's "Known architectural risks" for a concrete verified example.

These are tracked as a deliberate high-risk follow-up (dependency lock, tests, CI, flattening the
versioned builder/validator chain) — not addressed in the routine cleanup pass that produced this README.
