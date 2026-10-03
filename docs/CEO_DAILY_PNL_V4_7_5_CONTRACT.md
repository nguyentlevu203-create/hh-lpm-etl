# CEO Daily P&L v4.7.5 — GT / Nguyễn Xuân Mạnh split

## Business rule
- Source employee: `NV001` — `NGUYỄN XUÂN MẠNH`.
- Source data remains `channel_group=GT`.
- Reporting GT excludes NV001.
- NV001 is shown separately as `GT_NV001` / `GT - Nguyễn Xuân Mạnh` under sheet `01_GT`.
- Company TOTAL includes both GT-ex-NV001 and NV001, so no revenue or cost disappears.
- NV001 has normal GT cost policy: COGS from `unit_cogs`, logistics 2% Net Sales, backoffice 15% Net Sales, no platform/live/booking/packaging.
- NV001 advertising is forced to zero. All GT advertising remains on the normal GT row.

## Canonical v4.7.5 blocks
Renderers must use these instead of inherited v4.7.4 GT blocks:
- `reporting_mtd_daily_detail_by_channel_v4_7_5`
- `reporting_previous_mtd_daily_detail_by_channel_v4_7_5`
- `reporting_mtd_pnl_comparison_by_channel_v4_7_5`
- `reporting_daily_pnl_by_channel_map_v4_7_5`
- `reporting_target_progress_v4_7_5`
- `reporting_sku_daily_fact_v4_7_5`
- `reporting_sku_inventory_summary_v4_7_5`
- `reporting_top_20_sku_by_channel_v4_7_5`
- `reporting_gift_sku_mtd_by_channel_v4_7_5`
- `gt_special_employee_v4_7_5`

The old v4.7.4 blocks remain in the JSON only for backward compatibility and still contain NV001 inside GT.

## Reconciliation gates
The package fails when:
1. `legacy GT != displayed GT + NV001` for any material P&L field;
2. company total changes after the split;
3. NV001 ads is not zero;
4. DB rows with `employee_code=NV001` contain a different employee name.
