BEGIN;

-- HH Control Tower v2.12 - Nhanh shipping / BO / return fee rules
-- Safe DB scope:
-- - No ALTER TABLE
-- - No INSERT/UPDATE/DELETE data
-- - Does not modify raw tables
-- - Creates a Nhanh-only corrected scorecard view and redirects the package view to use it.
--
-- Business rules:
-- 1) nhanh.orders.shipping_fee must already be produced by nhanh_etl.py v2.12 as:
--      "Phí vận chuyển" - "Phí ship báo khách" + 25,000 if exact status = 'Đã hoàn'
-- 2) No BO for exact statuses: 'Đã hủy', 'Hệ Thống hủy', 'Đã hoàn', 'Khách hủy'
-- 3) For exact status 'Đã hoàn', the 25,000 return fee is included in nhanh.orders.shipping_fee by ETL v2.12

SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE OR REPLACE VIEW control_tower.v_scorecard_nhanh_daily_v2_12 AS
WITH nhanh_item AS (
    SELECT
        i.order_id,
        SUM(COALESCE(i.quantity,0) * COALESCE(i.price,0))::numeric(18,2) AS gross_sales,
        SUM(COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0))::numeric(18,2) AS cogs,
        SUM(COALESCE(i.quantity,0))::numeric(18,2) AS qty
    FROM nhanh.order_items i
    GROUP BY i.order_id
),
nhanh_order AS (
    SELECT
        o.order_date::date AS report_date,
        'NHANH'::text AS channel,
        SUM(COALESCE(i.gross_sales,0))::numeric(18,2) AS gross_sales,
        SUM(COALESCE(o.order_value,0))::numeric(18,2) AS net_sales,
        NULL::numeric(18,2) AS discount_voucher_return,
        SUM(COALESCE(i.cogs,0))::numeric(18,2) AS cogs,
        NULL::numeric(18,2) AS platform_fees,
        SUM(COALESCE(o.shipping_fee,0))::numeric(18,2) AS shipping_return_fee,
        SUM(
            CASE
                WHEN TRIM(COALESCE(o.status, '')) IN ('Đã hủy', 'Hệ Thống hủy', 'Đã hoàn', 'Khách hủy')
                THEN 0
                ELSE GREATEST(COALESCE(o.order_value,0),0) * 0.15
            END
        )::numeric(18,2) AS backoffice_fee,
        NULL::numeric(18,2) AS booking_fee,
        SUM(COALESCE(i.qty,0) * 2000)::numeric(18,2) AS packaging_cost,
        NULL::numeric(18,2) AS live_cost,
        NULL::numeric(18,2) AS payout,
        COUNT(*)::bigint AS orders,
        COUNT(*) FILTER (
            WHERE TRIM(COALESCE(o.status, '')) NOT IN ('Đã hủy', 'Hệ Thống hủy', 'Đã hoàn', 'Khách hủy')
        )::bigint AS success_orders,
        COUNT(*) FILTER (
            WHERE TRIM(COALESCE(o.status, '')) IN ('Đã hủy', 'Hệ Thống hủy', 'Đã hoàn', 'Khách hủy')
        )::bigint AS cancelled_orders
    FROM nhanh.orders o
    LEFT JOIN nhanh_item i ON i.order_id = o.id
    GROUP BY o.order_date::date
),
nhanh_ads AS (
    SELECT
        ads_date::date AS report_date,
        SUM(COALESCE(cost_vnd_vat, cost_vnd, 0))::numeric(18,2) AS ads_cost
    FROM nhanh.ads_costs
    GROUP BY ads_date::date
)
SELECT
    o.report_date,
    o.channel,
    o.gross_sales,
    o.net_sales,
    o.discount_voucher_return,
    o.cogs,
    (o.net_sales - o.cogs)::numeric(18,2) AS gross_margin,
    ROUND((o.net_sales - o.cogs) / NULLIF(o.net_sales,0) * 100, 2) AS gm_pct,
    o.platform_fees,
    o.shipping_return_fee,
    COALESCE(a.ads_cost,0)::numeric(18,2) AS ads_cost,
    o.live_cost,
    o.booking_fee,
    o.backoffice_fee,
    o.packaging_cost,
    o.payout,
    (
        o.net_sales
        - o.cogs
        - COALESCE(o.shipping_return_fee,0)
        - COALESCE(o.backoffice_fee,0)
        - COALESCE(o.packaging_cost,0)
        - COALESCE(a.ads_cost,0)
    )::numeric(18,2) AS profit,
    ROUND((
        o.net_sales
        - o.cogs
        - COALESCE(o.shipping_return_fee,0)
        - COALESCE(o.backoffice_fee,0)
        - COALESCE(o.packaging_cost,0)
        - COALESCE(a.ads_cost,0)
    ) / NULLIF(o.net_sales,0) * 100, 2) AS profit_margin_pct,
    ROUND(COALESCE(a.ads_cost,0) / NULLIF(o.net_sales,0) * 100, 2) AS ads_to_sales_pct,
    o.orders,
    o.success_orders,
    o.cancelled_orders,
    ROUND(o.cancelled_orders::numeric / NULLIF(o.orders,0) * 100, 2) AS cancellation_rate_pct,
    jsonb_build_object(
        'gross_sales', 'nhanh.order_items.quantity * nhanh.order_items.price',
        'net_sales', 'nhanh.orders.order_value from exact Excel header Giá trị đơn hàng',
        'discount_voucher_return', 'not_available: Nhanh file does not provide discount/voucher/return split',
        'cogs', 'nhanh.order_items.quantity * unit_cogs from dim.product_costs',
        'platform_fees', 'not_applicable',
        'shipping_return_fee', 'v2.12: nhanh.orders.shipping_fee = Phí vận chuyển - Phí ship báo khách + 25,000 if exact status Đã hoàn',
        'ads_cost', 'nhanh.ads_costs.cost_vnd_vat if generated, else cost_vnd',
        'live_cost', 'not_applicable',
        'booking_fee', 'not_applicable',
        'backoffice_fee', 'v2.12: 15% positive net order value except exact statuses Đã hủy/Hệ Thống hủy/Đã hoàn/Khách hủy = 0',
        'packaging_cost', 'rule: 2,000 VND * item quantity',
        'payout', 'not_applicable'
    ) AS source_map,
    30 AS channel_sort
FROM nhanh_order o
LEFT JOIN nhanh_ads a ON a.report_date = o.report_date;

COMMENT ON VIEW control_tower.v_scorecard_nhanh_daily_v2_12 IS
'Nhanh scorecard v2.12. Shipping from ETL v2.12: Phí vận chuyển - Phí ship báo khách + 25k for exact Đã hoàn. BO = 0 for exact Đã hủy/Hệ Thống hủy/Đã hoàn/Khách hủy.';

CREATE OR REPLACE VIEW control_tower.v_scorecard_by_channel_daily_for_package AS
WITH fixed AS (
    SELECT *
    FROM control_tower.v_scorecard_by_channel_daily
    WHERE channel <> 'NHANH'
    UNION ALL
    SELECT *
    FROM control_tower.v_scorecard_nhanh_daily_v2_12
)
SELECT
    report_date,
    channel,
    COALESCE(gross_sales, 0)::numeric(18,2) AS gross_sales,
    COALESCE(net_sales, 0)::numeric(18,2) AS net_sales,
    COALESCE(discount_voucher_return, 0)::numeric(18,2) AS discount_voucher_return,
    COALESCE(cogs, 0)::numeric(18,2) AS cogs,
    COALESCE(gross_margin, 0)::numeric(18,2) AS gross_margin,
    COALESCE(gm_pct, 0)::numeric(18,2) AS gm_pct,
    COALESCE(platform_fees, 0)::numeric(18,2) AS platform_fees,
    COALESCE(shipping_return_fee, 0)::numeric(18,2) AS shipping_return_fee,
    COALESCE(ads_cost, 0)::numeric(18,2) AS ads_cost,
    COALESCE(live_cost, 0)::numeric(18,2) AS live_cost,
    COALESCE(booking_fee, 0)::numeric(18,2) AS booking_fee,
    COALESCE(backoffice_fee, 0)::numeric(18,2) AS backoffice_fee,
    COALESCE(packaging_cost, 0)::numeric(18,2) AS packaging_cost,
    COALESCE(payout, 0)::numeric(18,2) AS payout,
    COALESCE(profit, 0)::numeric(18,2) AS profit,
    COALESCE(profit_margin_pct, 0)::numeric(18,2) AS profit_margin_pct,
    COALESCE(ads_to_sales_pct, 0)::numeric(18,2) AS ads_to_sales_pct,
    COALESCE(orders, 0)::bigint AS orders,
    COALESCE(success_orders, 0)::bigint AS success_orders,
    COALESCE(cancelled_orders, 0)::bigint AS cancelled_orders,
    COALESCE(cancellation_rate_pct, 0)::numeric(18,2) AS cancellation_rate_pct,
    source_map || jsonb_build_object(
        'ceo_package_policy',
        'exact_numbers_v2_12: use corrected Nhanh shipping/BO/return-fee rules; unavailable/not-applicable numeric fields are 0; keep filtered values as-is'
    ) AS source_map,
    channel_sort
FROM fixed;

COMMENT ON VIEW control_tower.v_scorecard_by_channel_daily_for_package IS
'CEO/package scorecard view v2.12. Nhanh uses corrected shipping/BO/return fee rules; other channels preserved from base scorecard view.';

COMMIT;
