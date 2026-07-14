-- 1) Check Shopee extra daily quality
SELECT
    report_date,
    shop_label,
    order_rows,
    extra_rows,
    matched_rows,
    orders_missing_extra,
    extra_without_order,
    orders_gross_sales,
    extra_gross_sales,
    seller_revenue,
    seller_discount,
    platform_voucher,
    total_discount_voucher,
    gross_sales_gap,
    extra_coverage_pct
FROM control_tower.v_shopee_gross_sales_quality_daily
WHERE report_date = '2026-05-20'
ORDER BY shop_label;

-- 2) Check Shopee scorecard
SELECT
    channel,
    gross_sales,
    net_sales,
    discount_voucher_return,
    cogs,
    gross_margin,
    gm_pct,
    platform_fees,
    shipping_return_fee,
    ads_cost,
    packaging_cost,
    payout,
    profit,
    profit_margin_pct
FROM control_tower.v_scorecard_by_channel_daily_for_package
WHERE report_date = '2026-05-20'
  AND channel = 'SHOPEE';

-- 3) Check all scorecard channels
SELECT
    report_date,
    channel,
    gross_sales,
    net_sales,
    discount_voucher_return,
    cogs,
    gross_margin,
    gm_pct,
    platform_fees,
    shipping_return_fee,
    ads_cost,
    live_cost,
    booking_fee,
    backoffice_fee,
    packaging_cost,
    payout,
    profit,
    profit_margin_pct
FROM control_tower.v_scorecard_by_channel_daily_for_package
WHERE report_date = '2026-05-20'
ORDER BY channel_sort;
