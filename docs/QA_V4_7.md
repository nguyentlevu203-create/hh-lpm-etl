# QA v4.7

## Offline/self-test gates

- Python compile: v4.7 builder/validator/checker.
- Builder target fixture: observed August mapping produces leaf TOTAL target 4.13b and ECOM reconciliation gap 0.
- SKU fixture: sold + gift = total push and mapped current stock remains the inventory snapshot value.
- Validator P&L fixture: Gross/Net/COGS/GM1/CM1/CM2/Profit equations.
- Calendar fixture: previous-month same-day clamps correctly.
- N/D scanner fixture.

## Production gates

A production package must also pass:

1. Full v4.6.1 validator.
2. Five leaf target rows when targets are required.
3. TOTAL target = five leaf targets; ECOM is parent only.
4. Target MTD actual = canonical monthly P&L Net Sales.
5. Full previous-month same-day P&L for five channels.
6. Every SKU fact: `sold_qty + gift_qty = total_push_qty`.
7. Every SKU fact: `sold_cogs + gift_cogs = total_push_cogs`.
8. Inventory snapshot `<= report_date`.
9. Inventory total = lot total = warehouse total = unique inventory SKU total.
10. Mapped SKU stock equals `inventory_sku_master` stock.
11. `near_date_pushed_qty` stays null in v4.7.
12. No `N/D`, `N/A`, EBITDA.
13. Excel sheet contract contains exactly seven sheets.

## Known intentional warnings until source data is added

- `target_cm2` is null if `ops.monthly_sales_targets.target_cm2` has not been added/populated.
- Daily target fields are null if `ops.daily_sales_targets` is absent/unpopulated.
- Near-date pushed quantity is null until v4.8 lot attribution.
- Composite marketplace SKUs may be inventory-unmapped until a component allocation master is created.
- Inventory P1/P2/P3/near-date alert classifications depend on the existing DB view; v4.7 will not duplicate undocumented thresholds in Python.
