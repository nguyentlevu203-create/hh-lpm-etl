# CEO Daily P&L v4.6 — one-pass source upgrade

## What v4.6 fixes

### 1. Channel-specific cancellation semantics

- **Shopee**: cancelled only when normalized raw status is exactly `Đã hủy`.
- **TikTok**: cancelled only when normalized raw status is exactly `Canceled`.
- **Nhanh**: cancelled only when exact trimmed raw status is one of:
  - `Đã hủy`
  - `Hệ Thống hủy`
  - `Khách hủy`
  - `HVC hủy`

Nhanh return/failure statuses are separate and **not** added to `cancelled_orders`:

- Return lifecycle: `Đang hoàn`, `Xác nhận hoàn`, `Đã hoàn`
- Failure: `Thất bại`

The existing Nhanh +25,000 VND shipping rule remains only for exact `Đã hoàn`.
The existing Nhanh BO logic is preserved except `HVC hủy` is added as a cancellation alias. v4.6 does not invent new BO treatment for `Đang hoàn`, `Xác nhận hoàn` or `Thất bại`.

### 2. Nhanh parent daily data is first-class

`mtd_daily_detail_by_channel.NHANH` is rebuilt from direct Nhanh source tables and contains the same daily parent-level structure used for the other e-commerce channels, including:

- Gross all statuses
- Gross non-cancelled
- Gross cancelled
- Net Sales
- Orders / non-cancelled / cancelled / cancellation rate
- Literal `Thành công` count
- Return-status counts
- Failure-status count
- Sold COGS / gift COGS / total COGS
- GM1 / CM1 / CM2 / Profit
- Shipping, packaging, ads and backoffice

The package also exposes an explicit alias `nhanh_parent_daily_detail` for AI/Excel renderers.

The seven exact Nhanh leaf channels are retained in `nhanh_leaf_channel_map` and `nhanh_leaf_daily_detail`. Parent and leaf reconcile by construction; `_UNALLOCATED` remains an internal cost bucket and must never render as a sales channel.

### 3. Growth blocks are harmonized for Nhanh too

Weekly/monthly Gross, order counts and cancelled order counts for Nhanh use the same direct daily source, so the daily report and inherited growth blocks cannot silently disagree on cancellation counts.

## Bundle layout

Overlay the bundle at repo root. It adds:

- `scripts/build_ceo_daily_pnl_package_v4_6.py`
- `scripts/validate_ceo_daily_pnl_package_v4_6.py`
- `tools/apply_ceo_daily_pnl_v4_6_upgrade.py`
- `sql/audit_ceo_daily_pnl_v4_6_status_contract.sql`
- `install_ceo_daily_pnl_v4_6.ps1`
- `run_daily_pnl_v4_6.ps1`

v4.5 is intentionally kept for rollback.

## Installation

From repo root:

```powershell
.\.venv\Scripts\Activate.ps1

python .\tools\apply_ceo_daily_pnl_v4_6_upgrade.py --self-test

.\install_ceo_daily_pnl_v4_6.ps1
```

The first installer run is check-only. If it reports no unknown layout:

```powershell
.\install_ceo_daily_pnl_v4_6.ps1 -Apply
```

The patcher creates backups with suffix:

```text
.bak_v4_6_status_contract
```

## Historical re-import requirement

### Shopee and TikTok — required

The marketplace ETLs use `is_cancelled` inside fee/COGS/backoffice/payout derivations, so changing only the stored boolean is unsafe.

After applying v4.6, restore the relevant order + affiliate + settlement source exports to the input folders and re-run the ETLs for the full comparison window used by the report.

For a report through 14/08/2026, use at least 01/07/2026–14/08/2026 if July is the prior-month comparison baseline.

Do not place overlapping duplicate exports for the same orders into the input folders.

### Nhanh — recommended, not required for report correctness

v4.6 recomputes Nhanh cancellation directly from raw `nhanh.orders.status`, so stale historical `nhanh.orders.is_cancelled` does not make the v4.6 package wrong. Re-import Nhanh when source exports are available so the stored flag is also cleaned for other consumers.

## Audit after re-import

Edit the date range at the top of:

```text
sql\audit_ceo_daily_pnl_v4_6_status_contract.sql
```

Then run:

```powershell
psql -h localhost -p 5433 -U postgres -d DuLieu -f .\sql\audit_ceo_daily_pnl_v4_6_status_contract.sql
```

Required marketplace gates:

```text
SHOPEE_FLAG_MISMATCH = 0
TIKTOK_FLAG_MISMATCH = 0
TIKTOK_CANCELED_FALSE = 0
```

`NHANH_FLAG_MISMATCH_INFORMATIONAL` may be non-zero before Nhanh historical re-import. The v4.6 package does not use that legacy flag.

## Build report

For 14/08/2026:

```powershell
.\run_daily_pnl_v4_6.ps1 `
  -ReportDate "2026-08-14" `
  -CompareMode "previous_day"
```

Expected output:

```text
outbox\packages\ceo_daily_pnl_package_v4_6_2026-08-14.json
```

Required end state:

```text
Data quality: PASS     # or WARNING only for explicitly non-blocking Nhanh legacy flag audit
VALIDATION PASSED
PASS: ...ceo_daily_pnl_package_v4_6_2026-08-14.json
```

Do not send the CEO workbook if validator fails.

## Important compatibility field

For Nhanh, `success_orders` remains the cross-channel compatibility field meaning **non-cancelled orders** so that:

```text
orders = success_orders + cancelled_orders
```

For literal Nhanh status `Thành công`, use:

```text
successful_orders_exact
```

This prevents the report from calling return/failure orders “cancelled” while preserving the existing cross-channel scorecard equation.
