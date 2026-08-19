# CEO Daily P&L v4.7.3 — HF1 — Previous-MTD Contract

## Period contract

For `report_date = D`:

- current MTD starts on day 1 of D's month and ends on D;
- previous MTD starts on day 1 of the previous month and ends on the same calendar day as D, clamped to the last valid day of that month.

Examples:

- 2026-08-18 -> current 2026-08-01..18, previous 2026-07-01..18.
- 2026-03-31 -> current 2026-03-01..31, previous 2026-02-01..28.

## Authoritative source

Previous MTD is rebuilt by `build_ceo_daily_pnl_package_v4_6_1.py` for the previous-period end date with `include_growth_context=False`. This preserves the exact production P&L/status semantics instead of deriving historical costs from current-period summaries.

## Required blocks

### `previous_mtd_daily_detail_by_channel`

Five keys: `GT`, `MT`, `NHANH`, `SHOPEE`, `TIKTOK`.

Each key must contain exactly one row for every date from previous month start through previous period end. No missing or duplicate dates are allowed.

### `previous_mtd_pnl_by_channel`

Aggregated full P&L for each channel, including Gross, deductions, Net Sales, sold/gift COGS, GM1, CM1, Ads, Affiliate, CM2, Backoffice and Profit where applicable.

### `previous_mtd_summary`

Company TOTAL. Every additive P&L field must reconcile to the sum of the five channel summaries within 1 VND.

### `mtd_pnl_comparison_by_channel`

For each channel:

- `current_period`
- `previous_period`
- `current_mtd`
- `previous_mtd`
- `pnl_lines`

The last P&L line must be `profit / LỢI NHUẬN`.

### `mtd_pnl_comparison_total`

Same contract as channel comparison at company TOTAL level.

## Strict gates

Validation fails if:

- prior-period date coverage is incomplete or duplicated;
- any previous-MTD channel summary does not equal its daily rows;
- company previous-MTD summary does not equal the five channel summaries;
- comparison periods are not exact same-day MTD periods;
- current comparison aggregate does not equal current `mtd_daily_detail_by_channel`;
- previous comparison aggregate does not equal `previous_mtd_pnl_by_channel`;
- a P&L comparison does not end at Profit/LỢI NHUẬN;
- any rendered P&L line contains EBITDA;
- the previous-MTD source package has `ERROR` / `can_send=false`.

## Explicit non-changes

v4.7.3 does not change v4.7.2 customer, Top20 SKU, inventory, target, cancellation, COGS, GM1, CM1, CM2 or Profit formulas.


## HF1 validator compatibility

HF1 removes the exact metadata key named after the forbidden metric from the v4.7.3 add-on contract. Older v4.6 prerequisite validators treated any exact key with that name as if it were a financial metric. The validator now checks actual `pnl_lines` semantically and ignores only the known legacy v4.7 metadata-string false-positive. Financial formulas and data are unchanged.
