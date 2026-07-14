-- ============================================================
-- HH CEO Growth Package v3.0 - Week & Month reporting views
-- Purpose:
--   1) Giữ nguyên package/báo cáo ngày hiện tại.
--   2) Tạo lớp view mới cho package báo cáo tuần + tháng.
--   3) Dữ liệu tuần/tháng lấy từ dữ liệu ngày rồi tổng hợp lên.
--   4) Có daily trend để agent vẽ biểu đồ cho CEO.
--   5) Không lấy tồn kho, không lấy đơn mẫu trong bộ view này.
--
-- Run:
--   psql -h localhost -p 5433 -U postgres -d DuLieu -f hh_growth_week_month_views_v3_0.sql
-- ============================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS control_tower;
CREATE SCHEMA IF NOT EXISTS mart;

-- ============================================================
-- 1) Daily base
-- IMPORTANT:
-- View này không sửa báo cáo ngày hiện tại. Nó chỉ đọc từ daily package view hiện có.
-- Nếu DB đang có view control_tower.v_scorecard_by_channel_daily_for_package,
-- hệ thống sẽ dùng view đó. Nếu không có thì dùng control_tower.v_scorecard_by_channel_daily.
-- Các cột optional không có sẽ được đưa về 0 bằng dynamic SQL.
-- ============================================================

DO $$
DECLARE
    src_schema text := 'control_tower';
    src_view text;
    src_reg regclass;

    c_report_date text;
    c_channel text;
    c_gross_sales text;
    c_net_sales text;
    c_orders text;
    c_success_orders text;
    c_cancelled_orders text;
    c_cogs text;
    c_gross_margin text;
    c_platform_fees text;
    c_shipping_return_fee text;
    c_backoffice_fee text;
    c_ads_cost text;
    c_live_cost text;
    c_booking_fee text;
    c_packaging_cost text;
    c_profit text;

    e_report_date text;
    e_channel text;
    e_gross_sales text;
    e_net_sales text;
    e_orders text;
    e_success_orders text;
    e_cancelled_orders text;
    e_cogs text;
    e_gross_margin text;
    e_platform_fees text;
    e_shipping_return_fee text;
    e_backoffice_fee text;
    e_ads_cost text;
    e_live_cost text;
    e_booking_fee text;
    e_packaging_cost text;
    e_profit text;
BEGIN
    IF to_regclass('control_tower.v_scorecard_by_channel_daily_for_package') IS NOT NULL THEN
        src_view := 'v_scorecard_by_channel_daily_for_package';
        src_reg := 'control_tower.v_scorecard_by_channel_daily_for_package'::regclass;
    ELSIF to_regclass('control_tower.v_scorecard_by_channel_daily') IS NOT NULL THEN
        src_view := 'v_scorecard_by_channel_daily';
        src_reg := 'control_tower.v_scorecard_by_channel_daily'::regclass;
    ELSE
        RAISE EXCEPTION 'Không tìm thấy source view scorecard daily.';
    END IF;

    SELECT quote_ident(column_name) INTO c_report_date
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['report_date','date','sale_date'])
    ORDER BY CASE lower(column_name) WHEN 'report_date' THEN 1 WHEN 'sale_date' THEN 2 ELSE 99 END
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_channel
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['channel','channel_group','sales_channel'])
    ORDER BY CASE lower(column_name) WHEN 'channel' THEN 1 WHEN 'channel_group' THEN 2 ELSE 99 END
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_gross_sales
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['gross_sales','gross_revenue'])
    ORDER BY CASE lower(column_name) WHEN 'gross_sales' THEN 1 ELSE 99 END
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_net_sales
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['net_sales','net_revenue','sales'])
    ORDER BY CASE lower(column_name) WHEN 'net_sales' THEN 1 WHEN 'net_revenue' THEN 2 ELSE 99 END
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_orders
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['orders','total_orders','order_count','orders_count'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_success_orders
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['success_orders','successful_orders','completed_orders'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_cancelled_orders
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['cancelled_orders','canceled_orders','cancelled','canceled','cancelled_order_count'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_cogs
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['cogs','cost_of_goods_sold','goods_cost'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_gross_margin
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['gross_margin','gm','gross_profit'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_platform_fees
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['platform_fees','platform_fee','platform_cost'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_shipping_return_fee
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['shipping_return_fee','shipping_fee','shipping_cost','return_shipping_fee','shipping_return_cost'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_backoffice_fee
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['backoffice_fee','backoffice_cost','bo_fee','bo_cost'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_ads_cost
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['ads_cost','ad_cost','advertising_cost','marketing_cost'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_live_cost
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['live_cost','live_fee'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_booking_fee
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['booking_fee','booking_cost'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_packaging_cost
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['packaging_cost','packaging_fee','packing_cost','packing_fee'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_profit
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['profit','net_profit'])
    LIMIT 1;

    IF c_report_date IS NULL THEN
        RAISE EXCEPTION 'Source view % thiếu cột ngày.', src_reg::text;
    END IF;
    IF c_channel IS NULL THEN
        RAISE EXCEPTION 'Source view % thiếu cột channel.', src_reg::text;
    END IF;
    IF c_net_sales IS NULL THEN
        RAISE EXCEPTION 'Source view % thiếu cột net_sales/net_revenue.', src_reg::text;
    END IF;

    e_report_date := format('(%s)::date', c_report_date);
    e_channel := format('COALESCE(NULLIF(TRIM((%s)::text), ''''), ''UNKNOWN'')', c_channel);
    e_net_sales := format('COALESCE(%s, 0)::numeric', c_net_sales);
    e_gross_sales := CASE WHEN c_gross_sales IS NULL THEN e_net_sales ELSE format('COALESCE(%s, 0)::numeric', c_gross_sales) END;
    e_orders := CASE WHEN c_orders IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_orders) END;
    e_cancelled_orders := CASE WHEN c_cancelled_orders IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_cancelled_orders) END;
    e_success_orders := CASE WHEN c_success_orders IS NULL THEN format('GREATEST((%s) - (%s), 0)::numeric', e_orders, e_cancelled_orders) ELSE format('COALESCE(%s, 0)::numeric', c_success_orders) END;
    e_cogs := CASE WHEN c_cogs IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_cogs) END;
    e_platform_fees := CASE WHEN c_platform_fees IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_platform_fees) END;
    e_shipping_return_fee := CASE WHEN c_shipping_return_fee IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_shipping_return_fee) END;
    e_backoffice_fee := CASE WHEN c_backoffice_fee IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_backoffice_fee) END;
    e_ads_cost := CASE WHEN c_ads_cost IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_ads_cost) END;
    e_live_cost := CASE WHEN c_live_cost IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_live_cost) END;
    e_booking_fee := CASE WHEN c_booking_fee IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_booking_fee) END;
    e_packaging_cost := CASE WHEN c_packaging_cost IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_packaging_cost) END;
    e_gross_margin := CASE WHEN c_gross_margin IS NULL THEN format('((%s) - (%s))::numeric', e_net_sales, e_cogs) ELSE format('COALESCE(%s, 0)::numeric', c_gross_margin) END;
    e_profit := CASE WHEN c_profit IS NULL THEN format('((%s) - (%s) - (%s) - (%s) - (%s) - (%s) - (%s) - (%s) - (%s))::numeric', e_net_sales, e_cogs, e_platform_fees, e_shipping_return_fee, e_backoffice_fee, e_ads_cost, e_live_cost, e_booking_fee, e_packaging_cost) ELSE format('COALESCE(%s, 0)::numeric', c_profit) END;

    EXECUTE format($view$
        CREATE OR REPLACE VIEW control_tower.v_growth_daily_base AS
        WITH normalized AS (
            SELECT
                %s AS report_date,
                %s AS channel,
                %s AS gross_sales,
                %s AS net_sales,
                %s AS orders,
                %s AS success_orders,
                %s AS cancelled_orders,
                %s AS cogs,
                %s AS gross_margin,
                %s AS platform_fees,
                %s AS shipping_return_fee,
                %s AS backoffice_fee,
                %s AS ads_cost,
                %s AS live_cost,
                %s AS booking_fee,
                %s AS packaging_cost,
                %s AS profit
            FROM %s
        ), daily AS (
            SELECT
                report_date,
                channel,
                SUM(gross_sales)::numeric AS gross_sales,
                SUM(net_sales)::numeric AS net_sales,
                SUM(orders)::numeric AS orders,
                SUM(success_orders)::numeric AS success_orders,
                SUM(cancelled_orders)::numeric AS cancelled_orders,
                SUM(cogs)::numeric AS cogs,
                SUM(gross_margin)::numeric AS gross_margin,
                SUM(platform_fees)::numeric AS platform_fees,
                SUM(shipping_return_fee)::numeric AS shipping_return_fee,
                SUM(backoffice_fee)::numeric AS backoffice_fee,
                SUM(ads_cost)::numeric AS ads_cost,
                SUM(live_cost)::numeric AS live_cost,
                SUM(booking_fee)::numeric AS booking_fee,
                SUM(packaging_cost)::numeric AS packaging_cost,
                SUM(profit)::numeric AS profit
            FROM normalized
            WHERE report_date IS NOT NULL
              AND UPPER(channel) NOT IN ('TOTAL','TONG','TỔNG')
              AND channel NOT IN ('Tổng','tổng')
            GROUP BY report_date, channel
        )
        SELECT
            report_date,
            channel,
            gross_sales,
            net_sales,
            orders,
            success_orders,
            cancelled_orders,
            cogs,
            gross_margin,
            CASE WHEN net_sales <> 0 THEN gross_margin / net_sales ELSE NULL END AS gm_pct,
            platform_fees,
            shipping_return_fee,
            backoffice_fee,
            ads_cost,
            live_cost,
            booking_fee,
            packaging_cost,
            profit,
            CASE WHEN net_sales <> 0 THEN profit / net_sales ELSE NULL END AS profit_margin_pct,
            CASE WHEN net_sales <> 0 THEN ads_cost / net_sales ELSE NULL END AS ads_sales_pct,
            CASE WHEN orders <> 0 THEN cancelled_orders / orders ELSE NULL END AS cancellation_rate
        FROM daily;
    $view$,
        e_report_date, e_channel, e_gross_sales, e_net_sales, e_orders, e_success_orders, e_cancelled_orders,
        e_cogs, e_gross_margin, e_platform_fees, e_shipping_return_fee, e_backoffice_fee, e_ads_cost,
        e_live_cost, e_booking_fee, e_packaging_cost, e_profit, src_reg::text
    );

    RAISE NOTICE 'Created control_tower.v_growth_daily_base from %', src_reg::text;
END $$;

-- ============================================================
-- 2) Weekly views
-- ============================================================

CREATE OR REPLACE VIEW control_tower.v_growth_weekly_daily_trend AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('week', report_date)::date AS week_start,
        report_date AS week_end
    FROM control_tower.v_growth_daily_base
), agg AS (
    SELECT
        a.as_of_date,
        a.week_start,
        a.week_end,
        b.report_date AS trend_date,
        CASE WHEN GROUPING(b.channel) = 1 THEN 'TOTAL' ELSE b.channel END AS channel,
        SUM(b.gross_sales)::numeric AS gross_sales,
        SUM(b.net_sales)::numeric AS net_sales,
        SUM(b.orders)::numeric AS orders,
        SUM(b.success_orders)::numeric AS success_orders,
        SUM(b.cancelled_orders)::numeric AS cancelled_orders,
        SUM(b.cogs)::numeric AS cogs,
        SUM(b.gross_margin)::numeric AS gross_margin,
        SUM(b.platform_fees)::numeric AS platform_fees,
        SUM(b.shipping_return_fee)::numeric AS shipping_return_fee,
        SUM(b.backoffice_fee)::numeric AS backoffice_fee,
        SUM(b.ads_cost)::numeric AS ads_cost,
        SUM(b.live_cost)::numeric AS live_cost,
        SUM(b.booking_fee)::numeric AS booking_fee,
        SUM(b.packaging_cost)::numeric AS packaging_cost,
        SUM(b.profit)::numeric AS profit
    FROM anchors a
    JOIN control_tower.v_growth_daily_base b
      ON b.report_date BETWEEN a.week_start AND a.week_end
    GROUP BY GROUPING SETS (
        (a.as_of_date, a.week_start, a.week_end, b.report_date, b.channel),
        (a.as_of_date, a.week_start, a.week_end, b.report_date)
    )
)
SELECT
    *,
    CASE WHEN net_sales <> 0 THEN gross_margin / net_sales ELSE NULL END AS gm_pct,
    CASE WHEN net_sales <> 0 THEN profit / net_sales ELSE NULL END AS profit_margin_pct,
    CASE WHEN net_sales <> 0 THEN ads_cost / net_sales ELSE NULL END AS ads_sales_pct,
    CASE WHEN orders <> 0 THEN cancelled_orders / orders ELSE NULL END AS cancellation_rate
FROM agg;

CREATE OR REPLACE VIEW control_tower.v_growth_weekly_summary AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('week', report_date)::date AS week_start,
        report_date AS week_end
    FROM control_tower.v_growth_daily_base
), agg AS (
    SELECT
        a.as_of_date,
        a.week_start,
        a.week_end,
        COUNT(DISTINCT b.report_date)::integer AS data_days,
        CASE WHEN GROUPING(b.channel) = 1 THEN 'TOTAL' ELSE b.channel END AS channel,
        SUM(b.gross_sales)::numeric AS gross_sales_wtd,
        SUM(b.net_sales)::numeric AS net_sales_wtd,
        SUM(b.orders)::numeric AS orders_wtd,
        SUM(b.success_orders)::numeric AS success_orders_wtd,
        SUM(b.cancelled_orders)::numeric AS cancelled_orders_wtd,
        SUM(b.cogs)::numeric AS cogs_wtd,
        SUM(b.gross_margin)::numeric AS gross_margin_wtd,
        SUM(b.platform_fees)::numeric AS platform_fees_wtd,
        SUM(b.shipping_return_fee)::numeric AS shipping_return_fee_wtd,
        SUM(b.backoffice_fee)::numeric AS backoffice_fee_wtd,
        SUM(b.ads_cost)::numeric AS ads_cost_wtd,
        SUM(b.live_cost)::numeric AS live_cost_wtd,
        SUM(b.booking_fee)::numeric AS booking_fee_wtd,
        SUM(b.packaging_cost)::numeric AS packaging_cost_wtd,
        SUM(b.profit)::numeric AS profit_wtd
    FROM anchors a
    JOIN control_tower.v_growth_daily_base b
      ON b.report_date BETWEEN a.week_start AND a.week_end
    GROUP BY GROUPING SETS (
        (a.as_of_date, a.week_start, a.week_end, b.channel),
        (a.as_of_date, a.week_start, a.week_end)
    )
)
SELECT
    *,
    CASE WHEN net_sales_wtd <> 0 THEN gross_margin_wtd / net_sales_wtd ELSE NULL END AS gm_pct_wtd,
    CASE WHEN net_sales_wtd <> 0 THEN profit_wtd / net_sales_wtd ELSE NULL END AS profit_margin_pct_wtd,
    CASE WHEN net_sales_wtd <> 0 THEN ads_cost_wtd / net_sales_wtd ELSE NULL END AS ads_sales_pct_wtd,
    CASE WHEN orders_wtd <> 0 THEN cancelled_orders_wtd / orders_wtd ELSE NULL END AS cancellation_rate_wtd
FROM agg;

CREATE OR REPLACE VIEW control_tower.v_growth_weekly_by_channel AS
SELECT *
FROM control_tower.v_growth_weekly_summary
WHERE channel <> 'TOTAL';

CREATE OR REPLACE VIEW control_tower.v_growth_weekly_comparison AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('week', report_date)::date AS week_start,
        report_date AS week_end,
        (date_trunc('week', report_date)::date - INTERVAL '7 days')::date AS prev_week_start,
        (report_date - INTERVAL '7 days')::date AS prev_week_end
    FROM control_tower.v_growth_daily_base
), periods AS (
    SELECT as_of_date, 'current'::text AS period_name, week_start AS period_start, week_end AS period_end
    FROM anchors
    UNION ALL
    SELECT as_of_date, 'previous'::text AS period_name, prev_week_start AS period_start, prev_week_end AS period_end
    FROM anchors
), agg AS (
    SELECT
        p.as_of_date,
        p.period_name,
        CASE WHEN GROUPING(b.channel) = 1 THEN 'TOTAL' ELSE b.channel END AS channel,
        COUNT(DISTINCT b.report_date)::integer AS data_days,
        COALESCE(SUM(b.gross_sales), 0)::numeric AS gross_sales,
        COALESCE(SUM(b.net_sales), 0)::numeric AS net_sales,
        COALESCE(SUM(b.orders), 0)::numeric AS orders,
        COALESCE(SUM(b.success_orders), 0)::numeric AS success_orders,
        COALESCE(SUM(b.cancelled_orders), 0)::numeric AS cancelled_orders,
        COALESCE(SUM(b.cogs), 0)::numeric AS cogs,
        COALESCE(SUM(b.gross_margin), 0)::numeric AS gross_margin,
        COALESCE(SUM(b.platform_fees), 0)::numeric AS platform_fees,
        COALESCE(SUM(b.shipping_return_fee), 0)::numeric AS shipping_return_fee,
        COALESCE(SUM(b.backoffice_fee), 0)::numeric AS backoffice_fee,
        COALESCE(SUM(b.ads_cost), 0)::numeric AS ads_cost,
        COALESCE(SUM(b.live_cost), 0)::numeric AS live_cost,
        COALESCE(SUM(b.booking_fee), 0)::numeric AS booking_fee,
        COALESCE(SUM(b.packaging_cost), 0)::numeric AS packaging_cost,
        COALESCE(SUM(b.profit), 0)::numeric AS profit
    FROM periods p
    LEFT JOIN control_tower.v_growth_daily_base b
      ON b.report_date BETWEEN p.period_start AND p.period_end
    GROUP BY GROUPING SETS (
        (p.as_of_date, p.period_name, b.channel),
        (p.as_of_date, p.period_name)
    )
), curr AS (
    SELECT * FROM agg WHERE period_name = 'current'
), prev AS (
    SELECT * FROM agg WHERE period_name = 'previous'
)
SELECT
    c.as_of_date,
    date_trunc('week', c.as_of_date)::date AS current_week_start,
    c.as_of_date AS current_week_end,
    (date_trunc('week', c.as_of_date)::date - INTERVAL '7 days')::date AS previous_week_start,
    (c.as_of_date - INTERVAL '7 days')::date AS previous_week_end,
    c.channel,
    COALESCE(p.data_days, 0) > 0 AS comparison_available,
    CASE WHEN COALESCE(p.data_days, 0) > 0 THEN NULL ELSE 'Chưa đủ dữ liệu cùng kỳ tuần trước' END AS comparison_note,

    c.gross_sales AS gross_sales_wtd,
    COALESCE(p.gross_sales, 0) AS gross_sales_prev_wtd,
    c.gross_sales - COALESCE(p.gross_sales, 0) AS gross_sales_delta,
    CASE WHEN COALESCE(p.gross_sales, 0) <> 0 THEN (c.gross_sales - p.gross_sales) / p.gross_sales ELSE NULL END AS gross_sales_delta_pct,

    c.net_sales AS net_sales_wtd,
    COALESCE(p.net_sales, 0) AS net_sales_prev_wtd,
    c.net_sales - COALESCE(p.net_sales, 0) AS net_sales_delta,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN (c.net_sales - p.net_sales) / p.net_sales ELSE NULL END AS net_sales_delta_pct,

    c.orders AS orders_wtd,
    COALESCE(p.orders, 0) AS orders_prev_wtd,
    c.orders - COALESCE(p.orders, 0) AS orders_delta,
    CASE WHEN COALESCE(p.orders, 0) <> 0 THEN (c.orders - p.orders) / p.orders ELSE NULL END AS orders_delta_pct,

    c.cogs AS cogs_wtd,
    COALESCE(p.cogs, 0) AS cogs_prev_wtd,
    c.cogs - COALESCE(p.cogs, 0) AS cogs_delta,

    c.gross_margin AS gross_margin_wtd,
    COALESCE(p.gross_margin, 0) AS gross_margin_prev_wtd,
    c.gross_margin - COALESCE(p.gross_margin, 0) AS gross_margin_delta,
    CASE WHEN c.net_sales <> 0 THEN c.gross_margin / c.net_sales ELSE NULL END AS gm_pct_wtd,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN p.gross_margin / p.net_sales ELSE NULL END AS gm_pct_prev_wtd,

    c.ads_cost AS ads_cost_wtd,
    COALESCE(p.ads_cost, 0) AS ads_cost_prev_wtd,
    c.ads_cost - COALESCE(p.ads_cost, 0) AS ads_cost_delta,
    CASE WHEN c.net_sales <> 0 THEN c.ads_cost / c.net_sales ELSE NULL END AS ads_sales_pct_wtd,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN p.ads_cost / p.net_sales ELSE NULL END AS ads_sales_pct_prev_wtd,

    c.profit AS profit_wtd,
    COALESCE(p.profit, 0) AS profit_prev_wtd,
    c.profit - COALESCE(p.profit, 0) AS profit_delta,
    CASE WHEN COALESCE(p.profit, 0) <> 0 THEN (c.profit - p.profit) / p.profit ELSE NULL END AS profit_delta_pct,
    CASE WHEN c.net_sales <> 0 THEN c.profit / c.net_sales ELSE NULL END AS profit_margin_pct_wtd,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN p.profit / p.net_sales ELSE NULL END AS profit_margin_pct_prev_wtd,

    c.cancelled_orders AS cancelled_orders_wtd,
    COALESCE(p.cancelled_orders, 0) AS cancelled_orders_prev_wtd,
    CASE WHEN c.orders <> 0 THEN c.cancelled_orders / c.orders ELSE NULL END AS cancellation_rate_wtd,
    CASE WHEN COALESCE(p.orders, 0) <> 0 THEN p.cancelled_orders / p.orders ELSE NULL END AS cancellation_rate_prev_wtd
FROM curr c
LEFT JOIN prev p
  ON p.as_of_date = c.as_of_date
 AND p.channel = c.channel;

-- ============================================================
-- 3) Monthly views
-- ============================================================

CREATE OR REPLACE VIEW control_tower.v_growth_monthly_daily_trend AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('month', report_date)::date AS month_start,
        report_date AS month_end
    FROM control_tower.v_growth_daily_base
), agg AS (
    SELECT
        a.as_of_date,
        a.month_start,
        a.month_end,
        b.report_date AS trend_date,
        CASE WHEN GROUPING(b.channel) = 1 THEN 'TOTAL' ELSE b.channel END AS channel,
        SUM(b.gross_sales)::numeric AS gross_sales,
        SUM(b.net_sales)::numeric AS net_sales,
        SUM(b.orders)::numeric AS orders,
        SUM(b.success_orders)::numeric AS success_orders,
        SUM(b.cancelled_orders)::numeric AS cancelled_orders,
        SUM(b.cogs)::numeric AS cogs,
        SUM(b.gross_margin)::numeric AS gross_margin,
        SUM(b.platform_fees)::numeric AS platform_fees,
        SUM(b.shipping_return_fee)::numeric AS shipping_return_fee,
        SUM(b.backoffice_fee)::numeric AS backoffice_fee,
        SUM(b.ads_cost)::numeric AS ads_cost,
        SUM(b.live_cost)::numeric AS live_cost,
        SUM(b.booking_fee)::numeric AS booking_fee,
        SUM(b.packaging_cost)::numeric AS packaging_cost,
        SUM(b.profit)::numeric AS profit
    FROM anchors a
    JOIN control_tower.v_growth_daily_base b
      ON b.report_date BETWEEN a.month_start AND a.month_end
    GROUP BY GROUPING SETS (
        (a.as_of_date, a.month_start, a.month_end, b.report_date, b.channel),
        (a.as_of_date, a.month_start, a.month_end, b.report_date)
    )
)
SELECT
    *,
    CASE WHEN net_sales <> 0 THEN gross_margin / net_sales ELSE NULL END AS gm_pct,
    CASE WHEN net_sales <> 0 THEN profit / net_sales ELSE NULL END AS profit_margin_pct,
    CASE WHEN net_sales <> 0 THEN ads_cost / net_sales ELSE NULL END AS ads_sales_pct,
    CASE WHEN orders <> 0 THEN cancelled_orders / orders ELSE NULL END AS cancellation_rate
FROM agg;

CREATE OR REPLACE VIEW control_tower.v_growth_monthly_summary AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('month', report_date)::date AS month_start,
        report_date AS period_end,
        (date_trunc('month', report_date)::date + INTERVAL '1 month - 1 day')::date AS calendar_month_end
    FROM control_tower.v_growth_daily_base
), agg AS (
    SELECT
        a.as_of_date,
        a.month_start,
        a.period_end AS month_end,
        a.calendar_month_end,
        COUNT(DISTINCT b.report_date)::integer AS data_days,
        (a.period_end - a.month_start + 1)::integer AS days_elapsed,
        EXTRACT(day FROM a.calendar_month_end)::integer AS days_in_month,
        CASE WHEN GROUPING(b.channel) = 1 THEN 'TOTAL' ELSE b.channel END AS channel,
        SUM(b.gross_sales)::numeric AS gross_sales_mtd,
        SUM(b.net_sales)::numeric AS net_sales_mtd,
        SUM(b.orders)::numeric AS orders_mtd,
        SUM(b.success_orders)::numeric AS success_orders_mtd,
        SUM(b.cancelled_orders)::numeric AS cancelled_orders_mtd,
        SUM(b.cogs)::numeric AS cogs_mtd,
        SUM(b.gross_margin)::numeric AS gross_margin_mtd,
        SUM(b.platform_fees)::numeric AS platform_fees_mtd,
        SUM(b.shipping_return_fee)::numeric AS shipping_return_fee_mtd,
        SUM(b.backoffice_fee)::numeric AS backoffice_fee_mtd,
        SUM(b.ads_cost)::numeric AS ads_cost_mtd,
        SUM(b.live_cost)::numeric AS live_cost_mtd,
        SUM(b.booking_fee)::numeric AS booking_fee_mtd,
        SUM(b.packaging_cost)::numeric AS packaging_cost_mtd,
        SUM(b.profit)::numeric AS profit_mtd
    FROM anchors a
    JOIN control_tower.v_growth_daily_base b
      ON b.report_date BETWEEN a.month_start AND a.period_end
    GROUP BY GROUPING SETS (
        (a.as_of_date, a.month_start, a.period_end, a.calendar_month_end, b.channel),
        (a.as_of_date, a.month_start, a.period_end, a.calendar_month_end)
    )
)
SELECT
    *,
    CASE WHEN net_sales_mtd <> 0 THEN gross_margin_mtd / net_sales_mtd ELSE NULL END AS gm_pct_mtd,
    CASE WHEN net_sales_mtd <> 0 THEN profit_mtd / net_sales_mtd ELSE NULL END AS profit_margin_pct_mtd,
    CASE WHEN net_sales_mtd <> 0 THEN ads_cost_mtd / net_sales_mtd ELSE NULL END AS ads_sales_pct_mtd,
    CASE WHEN orders_mtd <> 0 THEN cancelled_orders_mtd / orders_mtd ELSE NULL END AS cancellation_rate_mtd,
    CASE WHEN days_elapsed > 0 THEN net_sales_mtd / days_elapsed ELSE NULL END AS run_rate_net_sales_per_day,
    CASE WHEN days_elapsed > 0 THEN profit_mtd / days_elapsed ELSE NULL END AS run_rate_profit_per_day,
    CASE WHEN days_elapsed > 0 THEN (net_sales_mtd / days_elapsed) * days_in_month ELSE NULL END AS forecast_net_sales_month_end,
    CASE WHEN days_elapsed > 0 THEN (profit_mtd / days_elapsed) * days_in_month ELSE NULL END AS forecast_profit_month_end
FROM agg;

CREATE OR REPLACE VIEW control_tower.v_growth_monthly_by_channel AS
WITH total AS (
    SELECT as_of_date, net_sales_mtd AS total_net_sales_mtd
    FROM control_tower.v_growth_monthly_summary
    WHERE channel = 'TOTAL'
)
SELECT
    c.*,
    CASE WHEN t.total_net_sales_mtd <> 0 THEN c.net_sales_mtd / t.total_net_sales_mtd ELSE NULL END AS channel_share_pct
FROM control_tower.v_growth_monthly_summary c
LEFT JOIN total t ON t.as_of_date = c.as_of_date
WHERE c.channel <> 'TOTAL';

CREATE OR REPLACE VIEW control_tower.v_growth_monthly_comparison AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('month', report_date)::date AS current_month_start,
        report_date AS current_period_end,
        (date_trunc('month', report_date)::date - INTERVAL '1 month')::date AS previous_month_start,
        LEAST(
            ((date_trunc('month', report_date)::date - INTERVAL '1 month')::date + (report_date - date_trunc('month', report_date)::date)),
            (date_trunc('month', report_date)::date - INTERVAL '1 day')::date
        ) AS previous_period_end
    FROM control_tower.v_growth_daily_base
), periods AS (
    SELECT as_of_date, 'current'::text AS period_name, current_month_start AS period_start, current_period_end AS period_end
    FROM anchors
    UNION ALL
    SELECT as_of_date, 'previous'::text AS period_name, previous_month_start AS period_start, previous_period_end AS period_end
    FROM anchors
), agg AS (
    SELECT
        p.as_of_date,
        p.period_name,
        CASE WHEN GROUPING(b.channel) = 1 THEN 'TOTAL' ELSE b.channel END AS channel,
        COUNT(DISTINCT b.report_date)::integer AS data_days,
        COALESCE(SUM(b.gross_sales), 0)::numeric AS gross_sales,
        COALESCE(SUM(b.net_sales), 0)::numeric AS net_sales,
        COALESCE(SUM(b.orders), 0)::numeric AS orders,
        COALESCE(SUM(b.success_orders), 0)::numeric AS success_orders,
        COALESCE(SUM(b.cancelled_orders), 0)::numeric AS cancelled_orders,
        COALESCE(SUM(b.cogs), 0)::numeric AS cogs,
        COALESCE(SUM(b.gross_margin), 0)::numeric AS gross_margin,
        COALESCE(SUM(b.platform_fees), 0)::numeric AS platform_fees,
        COALESCE(SUM(b.shipping_return_fee), 0)::numeric AS shipping_return_fee,
        COALESCE(SUM(b.backoffice_fee), 0)::numeric AS backoffice_fee,
        COALESCE(SUM(b.ads_cost), 0)::numeric AS ads_cost,
        COALESCE(SUM(b.live_cost), 0)::numeric AS live_cost,
        COALESCE(SUM(b.booking_fee), 0)::numeric AS booking_fee,
        COALESCE(SUM(b.packaging_cost), 0)::numeric AS packaging_cost,
        COALESCE(SUM(b.profit), 0)::numeric AS profit
    FROM periods p
    LEFT JOIN control_tower.v_growth_daily_base b
      ON b.report_date BETWEEN p.period_start AND p.period_end
    GROUP BY GROUPING SETS (
        (p.as_of_date, p.period_name, b.channel),
        (p.as_of_date, p.period_name)
    )
), curr AS (
    SELECT * FROM agg WHERE period_name = 'current'
), prev AS (
    SELECT * FROM agg WHERE period_name = 'previous'
)
SELECT
    c.as_of_date,
    date_trunc('month', c.as_of_date)::date AS current_month_start,
    c.as_of_date AS current_period_end,
    (date_trunc('month', c.as_of_date)::date - INTERVAL '1 month')::date AS previous_month_start,
    LEAST(
        ((date_trunc('month', c.as_of_date)::date - INTERVAL '1 month')::date + (c.as_of_date - date_trunc('month', c.as_of_date)::date)),
        (date_trunc('month', c.as_of_date)::date - INTERVAL '1 day')::date
    ) AS previous_period_end,
    c.channel,
    COALESCE(p.data_days, 0) > 0 AS comparison_available,
    CASE WHEN COALESCE(p.data_days, 0) > 0 THEN NULL ELSE 'Chưa đủ dữ liệu cùng kỳ tháng trước' END AS comparison_note,

    c.gross_sales AS gross_sales_mtd,
    COALESCE(p.gross_sales, 0) AS gross_sales_prev_mtd,
    c.gross_sales - COALESCE(p.gross_sales, 0) AS gross_sales_delta,
    CASE WHEN COALESCE(p.gross_sales, 0) <> 0 THEN (c.gross_sales - p.gross_sales) / p.gross_sales ELSE NULL END AS gross_sales_delta_pct,

    c.net_sales AS net_sales_mtd,
    COALESCE(p.net_sales, 0) AS net_sales_prev_mtd,
    c.net_sales - COALESCE(p.net_sales, 0) AS net_sales_delta,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN (c.net_sales - p.net_sales) / p.net_sales ELSE NULL END AS net_sales_delta_pct,

    c.orders AS orders_mtd,
    COALESCE(p.orders, 0) AS orders_prev_mtd,
    c.orders - COALESCE(p.orders, 0) AS orders_delta,
    CASE WHEN COALESCE(p.orders, 0) <> 0 THEN (c.orders - p.orders) / p.orders ELSE NULL END AS orders_delta_pct,

    c.cogs AS cogs_mtd,
    COALESCE(p.cogs, 0) AS cogs_prev_mtd,
    c.cogs - COALESCE(p.cogs, 0) AS cogs_delta,

    c.gross_margin AS gross_margin_mtd,
    COALESCE(p.gross_margin, 0) AS gross_margin_prev_mtd,
    c.gross_margin - COALESCE(p.gross_margin, 0) AS gross_margin_delta,
    CASE WHEN c.net_sales <> 0 THEN c.gross_margin / c.net_sales ELSE NULL END AS gm_pct_mtd,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN p.gross_margin / p.net_sales ELSE NULL END AS gm_pct_prev_mtd,

    c.ads_cost AS ads_cost_mtd,
    COALESCE(p.ads_cost, 0) AS ads_cost_prev_mtd,
    c.ads_cost - COALESCE(p.ads_cost, 0) AS ads_cost_delta,
    CASE WHEN c.net_sales <> 0 THEN c.ads_cost / c.net_sales ELSE NULL END AS ads_sales_pct_mtd,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN p.ads_cost / p.net_sales ELSE NULL END AS ads_sales_pct_prev_mtd,

    c.profit AS profit_mtd,
    COALESCE(p.profit, 0) AS profit_prev_mtd,
    c.profit - COALESCE(p.profit, 0) AS profit_delta,
    CASE WHEN COALESCE(p.profit, 0) <> 0 THEN (c.profit - p.profit) / p.profit ELSE NULL END AS profit_delta_pct,
    CASE WHEN c.net_sales <> 0 THEN c.profit / c.net_sales ELSE NULL END AS profit_margin_pct_mtd,
    CASE WHEN COALESCE(p.net_sales, 0) <> 0 THEN p.profit / p.net_sales ELSE NULL END AS profit_margin_pct_prev_mtd,

    c.cancelled_orders AS cancelled_orders_mtd,
    COALESCE(p.cancelled_orders, 0) AS cancelled_orders_prev_mtd,
    CASE WHEN c.orders <> 0 THEN c.cancelled_orders / c.orders ELSE NULL END AS cancellation_rate_mtd,
    CASE WHEN COALESCE(p.orders, 0) <> 0 THEN p.cancelled_orders / p.orders ELSE NULL END AS cancellation_rate_prev_mtd
FROM curr c
LEFT JOIN prev p ON p.as_of_date = c.as_of_date AND p.channel = c.channel;

CREATE OR REPLACE VIEW control_tower.v_growth_monthly_target_forecast AS
SELECT
    as_of_date,
    month_start,
    month_end,
    calendar_month_end,
    days_elapsed,
    days_in_month,
    channel,
    net_sales_mtd,
    profit_mtd,
    run_rate_net_sales_per_day,
    run_rate_profit_per_day,
    forecast_net_sales_month_end,
    forecast_profit_month_end,
    NULL::numeric AS target_net_sales,
    NULL::numeric AS target_profit,
    NULL::numeric AS target_completion_pct,
    NULL::numeric AS forecast_vs_target_gap
FROM control_tower.v_growth_monthly_summary;

-- ============================================================
-- 4) Cost breakdown views for charts
-- ============================================================

CREATE OR REPLACE VIEW control_tower.v_growth_cost_breakdown_weekly AS
SELECT
    s.as_of_date,
    s.week_start,
    s.week_end,
    s.channel,
    v.cost_type,
    v.amount::numeric AS amount,
    CASE WHEN s.net_sales_wtd <> 0 THEN v.amount / s.net_sales_wtd ELSE NULL END AS pct_of_net_sales
FROM control_tower.v_growth_weekly_summary s
CROSS JOIN LATERAL (
    VALUES
        ('Platform fees', s.platform_fees_wtd),
        ('Shipping / return fee', s.shipping_return_fee_wtd),
        ('Backoffice fee', s.backoffice_fee_wtd),
        ('Ads cost', s.ads_cost_wtd),
        ('Live cost', s.live_cost_wtd),
        ('Booking fee', s.booking_fee_wtd),
        ('Packaging cost', s.packaging_cost_wtd)
) AS v(cost_type, amount);

CREATE OR REPLACE VIEW control_tower.v_growth_cost_breakdown_monthly AS
SELECT
    s.as_of_date,
    s.month_start,
    s.month_end,
    s.channel,
    v.cost_type,
    v.amount::numeric AS amount,
    CASE WHEN s.net_sales_mtd <> 0 THEN v.amount / s.net_sales_mtd ELSE NULL END AS pct_of_net_sales
FROM control_tower.v_growth_monthly_summary s
CROSS JOIN LATERAL (
    VALUES
        ('Platform fees', s.platform_fees_mtd),
        ('Shipping / return fee', s.shipping_return_fee_mtd),
        ('Backoffice fee', s.backoffice_fee_mtd),
        ('Ads cost', s.ads_cost_mtd),
        ('Live cost', s.live_cost_mtd),
        ('Booking fee', s.booking_fee_mtd),
        ('Packaging cost', s.packaging_cost_mtd)
) AS v(cost_type, amount);

-- ============================================================
-- 5) Top SKU monthly sales level
--    Chỉ lấy mức độ bán tháng hiện tại.
--    Không lấy tồn kho, không lấy đơn mẫu, không so sánh tháng trước.
-- ============================================================

DO $$
DECLARE
    src_schema text := 'mart';
    src_view text;
    src_reg regclass;

    c_report_date text;
    c_channel text;
    c_sku_code text;
    c_ean text;
    c_product_name text;
    c_qty_sold text;
    c_net_sales text;

    e_report_date text;
    e_channel text;
    e_sku_code text;
    e_ean text;
    e_product_name text;
    e_qty_sold text;
    e_net_sales text;
BEGIN
    IF to_regclass('mart.v_sku_daily_sales_clean') IS NOT NULL THEN
        src_view := 'v_sku_daily_sales_clean';
        src_reg := 'mart.v_sku_daily_sales_clean'::regclass;
    ELSIF to_regclass('mart.v_sku_daily_sales') IS NOT NULL THEN
        src_view := 'v_sku_daily_sales';
        src_reg := 'mart.v_sku_daily_sales'::regclass;
    ELSE
        RAISE NOTICE 'Không tìm thấy mart.v_sku_daily_sales_clean hoặc mart.v_sku_daily_sales. Tạo Top SKU base rỗng để migration vẫn chạy.';
        EXECUTE $empty$
            CREATE OR REPLACE VIEW mart.v_growth_sku_daily_base AS
            SELECT
                NULL::date AS report_date,
                NULL::text AS channel,
                NULL::text AS sku_code,
                NULL::text AS ean,
                NULL::text AS product_name,
                0::numeric AS qty_sold,
                0::numeric AS net_sales
            WHERE false;
        $empty$;
        RETURN;
    END IF;

    SELECT quote_ident(column_name) INTO c_report_date
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['report_date','date','sale_date'])
    ORDER BY CASE lower(column_name) WHEN 'report_date' THEN 1 WHEN 'sale_date' THEN 2 ELSE 99 END
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_channel
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['channel','channel_group','sales_channel'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_sku_code
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['source_sku_code','sku_code','product_code','sku'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_ean
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['source_ean','ean','barcode','bar_code'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_product_name
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['source_product_name','product_name','name','item_name'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_qty_sold
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['qty_sold','quantity_sold','qty','quantity'])
    LIMIT 1;

    SELECT quote_ident(column_name) INTO c_net_sales
    FROM information_schema.columns
    WHERE table_schema = src_schema AND table_name = src_view
      AND lower(column_name) = ANY (ARRAY['net_sales','net_revenue','sales'])
    LIMIT 1;

    IF c_report_date IS NULL THEN
        RAISE EXCEPTION 'SKU source view % thiếu cột ngày.', src_reg::text;
    END IF;
    IF c_qty_sold IS NULL THEN
        RAISE EXCEPTION 'SKU source view % thiếu cột số lượng bán.', src_reg::text;
    END IF;
    IF c_sku_code IS NULL AND c_ean IS NULL AND c_product_name IS NULL THEN
        RAISE EXCEPTION 'SKU source view % thiếu mã SKU/EAN/tên hàng.', src_reg::text;
    END IF;

    e_report_date := format('(%s)::date', c_report_date);
    e_channel := CASE WHEN c_channel IS NULL THEN '''UNKNOWN''::text' ELSE format('COALESCE(NULLIF(TRIM((%s)::text), ''''), ''UNKNOWN'')', c_channel) END;
    e_sku_code := CASE WHEN c_sku_code IS NULL THEN 'NULL::text' ELSE format('NULLIF(TRIM((%s)::text), '''')', c_sku_code) END;
    e_ean := CASE WHEN c_ean IS NULL THEN 'NULL::text' ELSE format('NULLIF(TRIM((%s)::text), '''')', c_ean) END;
    e_product_name := CASE WHEN c_product_name IS NULL THEN 'NULL::text' ELSE format('NULLIF(TRIM((%s)::text), '''')', c_product_name) END;
    e_qty_sold := format('COALESCE(%s, 0)::numeric', c_qty_sold);
    e_net_sales := CASE WHEN c_net_sales IS NULL THEN '0::numeric' ELSE format('COALESCE(%s, 0)::numeric', c_net_sales) END;

    EXECUTE format($view$
        CREATE OR REPLACE VIEW mart.v_growth_sku_daily_base AS
        WITH normalized AS (
            SELECT
                %s AS report_date,
                %s AS channel,
                %s AS sku_code,
                %s AS ean,
                %s AS product_name,
                %s AS qty_sold,
                %s AS net_sales
            FROM %s
        )
        SELECT
            report_date,
            channel,
            sku_code,
            ean,
            product_name,
            SUM(qty_sold)::numeric AS qty_sold,
            SUM(net_sales)::numeric AS net_sales
        FROM normalized
        WHERE report_date IS NOT NULL
          AND COALESCE(sku_code, ean, product_name) IS NOT NULL
        GROUP BY report_date, channel, sku_code, ean, product_name;
    $view$, e_report_date, e_channel, e_sku_code, e_ean, e_product_name, e_qty_sold, e_net_sales, src_reg::text);

    RAISE NOTICE 'Created mart.v_growth_sku_daily_base from %', src_reg::text;
END $$;

CREATE OR REPLACE VIEW mart.v_growth_top_sku_monthly_sales_level AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('month', report_date)::date AS month_start,
        report_date AS month_end,
        (report_date - date_trunc('month', report_date)::date + 1)::integer AS days_elapsed
    FROM mart.v_growth_sku_daily_base
), agg AS (
    SELECT
        a.as_of_date,
        a.month_start,
        a.month_end,
        a.days_elapsed,
        COALESCE(b.sku_code, b.ean, b.product_name, 'UNKNOWN') AS sku_key,
        MIN(b.sku_code) AS sku_code,
        MIN(b.ean) AS ean,
        MIN(b.product_name) AS product_name,
        STRING_AGG(DISTINCT b.channel, ', ' ORDER BY b.channel) AS channels,
        SUM(b.qty_sold)::numeric AS qty_mtd,
        SUM(b.net_sales)::numeric AS net_sales_mtd
    FROM anchors a
    JOIN mart.v_growth_sku_daily_base b
      ON b.report_date BETWEEN a.month_start AND a.month_end
    GROUP BY a.as_of_date, a.month_start, a.month_end, a.days_elapsed, COALESCE(b.sku_code, b.ean, b.product_name, 'UNKNOWN')
), ranked AS (
    SELECT
        *,
        SUM(net_sales_mtd) OVER (PARTITION BY as_of_date) AS total_net_sales_mtd,
        DENSE_RANK() OVER (PARTITION BY as_of_date ORDER BY qty_mtd DESC NULLS LAST) AS rank_by_qty,
        DENSE_RANK() OVER (PARTITION BY as_of_date ORDER BY net_sales_mtd DESC NULLS LAST) AS rank_by_net_sales
    FROM agg
)
SELECT
    as_of_date,
    month_start,
    month_end,
    sku_key,
    sku_code,
    ean,
    product_name,
    channels,
    qty_mtd,
    net_sales_mtd,
    CASE WHEN total_net_sales_mtd <> 0 THEN net_sales_mtd / total_net_sales_mtd ELSE NULL END AS sales_share_pct,
    CASE WHEN days_elapsed > 0 THEN qty_mtd / days_elapsed ELSE NULL END AS avg_qty_per_day,
    rank_by_qty,
    rank_by_net_sales,
    CASE
        WHEN rank_by_qty <= 5 OR rank_by_net_sales <= 5 THEN 'Rất mạnh'
        WHEN rank_by_qty <= 15 OR rank_by_net_sales <= 15 THEN 'Tốt'
        ELSE 'Theo dõi'
    END AS sales_level
FROM ranked
WHERE rank_by_qty <= 50 OR rank_by_net_sales <= 50;

CREATE OR REPLACE VIEW mart.v_growth_top_sku_monthly_by_channel AS
WITH anchors AS (
    SELECT DISTINCT
        report_date AS as_of_date,
        date_trunc('month', report_date)::date AS month_start,
        report_date AS month_end,
        (report_date - date_trunc('month', report_date)::date + 1)::integer AS days_elapsed
    FROM mart.v_growth_sku_daily_base
), agg AS (
    SELECT
        a.as_of_date,
        a.month_start,
        a.month_end,
        a.days_elapsed,
        b.channel,
        COALESCE(b.sku_code, b.ean, b.product_name, 'UNKNOWN') AS sku_key,
        MIN(b.sku_code) AS sku_code,
        MIN(b.ean) AS ean,
        MIN(b.product_name) AS product_name,
        SUM(b.qty_sold)::numeric AS qty_mtd,
        SUM(b.net_sales)::numeric AS net_sales_mtd
    FROM anchors a
    JOIN mart.v_growth_sku_daily_base b
      ON b.report_date BETWEEN a.month_start AND a.month_end
    GROUP BY a.as_of_date, a.month_start, a.month_end, a.days_elapsed, b.channel, COALESCE(b.sku_code, b.ean, b.product_name, 'UNKNOWN')
), ranked AS (
    SELECT
        *,
        SUM(net_sales_mtd) OVER (PARTITION BY as_of_date, channel) AS channel_net_sales_mtd,
        DENSE_RANK() OVER (PARTITION BY as_of_date, channel ORDER BY qty_mtd DESC NULLS LAST) AS rank_by_qty,
        DENSE_RANK() OVER (PARTITION BY as_of_date, channel ORDER BY net_sales_mtd DESC NULLS LAST) AS rank_by_net_sales
    FROM agg
)
SELECT
    as_of_date,
    month_start,
    month_end,
    channel,
    sku_key,
    sku_code,
    ean,
    product_name,
    qty_mtd,
    net_sales_mtd,
    CASE WHEN channel_net_sales_mtd <> 0 THEN net_sales_mtd / channel_net_sales_mtd ELSE NULL END AS channel_sales_share_pct,
    CASE WHEN days_elapsed > 0 THEN qty_mtd / days_elapsed ELSE NULL END AS avg_qty_per_day,
    rank_by_qty,
    rank_by_net_sales,
    CASE
        WHEN rank_by_qty <= 5 OR rank_by_net_sales <= 5 THEN 'Rất mạnh'
        WHEN rank_by_qty <= 15 OR rank_by_net_sales <= 15 THEN 'Tốt'
        ELSE 'Theo dõi'
    END AS sales_level
FROM ranked
WHERE rank_by_qty <= 30 OR rank_by_net_sales <= 30;

-- ============================================================
-- 6) Quick checks - sửa ngày theo ngày muốn kiểm
-- ============================================================

-- SELECT * FROM control_tower.v_growth_weekly_summary
-- WHERE as_of_date = '2026-06-15' AND channel = 'TOTAL';

-- SELECT * FROM control_tower.v_growth_weekly_comparison
-- WHERE as_of_date = '2026-06-15' AND channel = 'TOTAL';

-- SELECT * FROM control_tower.v_growth_monthly_summary
-- WHERE as_of_date = '2026-06-15' AND channel = 'TOTAL';

-- SELECT * FROM control_tower.v_growth_monthly_comparison
-- WHERE as_of_date = '2026-06-15' AND channel = 'TOTAL';

-- SELECT * FROM mart.v_growth_top_sku_monthly_sales_level
-- WHERE as_of_date = '2026-06-15'
-- ORDER BY rank_by_qty
-- LIMIT 20;

COMMIT;
