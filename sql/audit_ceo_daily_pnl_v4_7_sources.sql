-- CEO Daily P&L v4.7 source audit (READ ONLY).
-- In psql you can set the report date before running:
--   \set report_date '2026-08-14'
-- If report_date is not set, replace :'report_date' manually in your SQL client.

\echo '=== 1. MONTHLY TARGETS ==='
SELECT
    target_month,
    display_order,
    channel_code,
    channel_name,
    parent_channel,
    target_revenue,
    CASE WHEN EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='ops' AND table_name='monthly_sales_targets' AND column_name='target_cm2'
    ) THEN 'target_cm2 column exists' ELSE 'target_cm2 column missing' END AS cm2_schema_status,
    is_enabled
FROM ops.monthly_sales_targets
WHERE target_month = date_trunc('month', :'report_date'::date)::date
ORDER BY display_order, channel_code;

\echo '=== 2. TARGET ECOM RECONCILIATION ==='
WITH t AS (
    SELECT upper(btrim(channel_code)) AS channel_code, SUM(COALESCE(target_revenue,0)) AS target_revenue
    FROM ops.monthly_sales_targets
    WHERE target_month = date_trunc('month', :'report_date'::date)::date
      AND COALESCE(is_enabled,true)
    GROUP BY 1
)
SELECT
    COALESCE((SELECT target_revenue FROM t WHERE channel_code='ECOM'),0) AS ecom_target,
    COALESCE((SELECT target_revenue FROM t WHERE channel_code='SHOPEE'),0)
      + COALESCE((SELECT target_revenue FROM t WHERE channel_code='TIKTOK'),0) AS shopee_plus_tiktok,
    COALESCE((SELECT target_revenue FROM t WHERE channel_code='ECOM'),0)
      - COALESCE((SELECT target_revenue FROM t WHERE channel_code='SHOPEE'),0)
      - COALESCE((SELECT target_revenue FROM t WHERE channel_code='TIKTOK'),0) AS reconciliation_gap;

\echo '=== 3. INVENTORY SNAPSHOT DATES <= REPORT DATE ==='
SELECT snapshot_date, warehouse, COUNT(*) AS lot_rows, SUM(stock_qty) AS stock_qty
FROM inventory.stock_lot_snapshots
WHERE snapshot_date <= :'report_date'::date
GROUP BY snapshot_date, warehouse
ORDER BY snapshot_date DESC, warehouse;

\echo '=== 4. LATEST SNAPSHOT USED BY v4.7 ==='
WITH d AS (
    SELECT MAX(snapshot_date)::date AS snapshot_date
    FROM inventory.stock_lot_snapshots
    WHERE snapshot_date <= :'report_date'::date
)
SELECT d.snapshot_date,
       :'report_date'::date - d.snapshot_date AS freshness_days,
       COUNT(s.*) AS lot_rows,
       SUM(COALESCE(s.stock_qty,0)) AS total_stock
FROM d
LEFT JOIN inventory.stock_lot_snapshots s ON s.snapshot_date=d.snapshot_date
GROUP BY d.snapshot_date;

\echo '=== 5. INVENTORY TABLE COLUMNS ==='
SELECT column_name, data_type
FROM information_schema.columns
WHERE table_schema='inventory' AND table_name='stock_lot_snapshots'
ORDER BY ordinal_position;

\echo '=== 6. INVENTORY ALERT VIEW DEFINITION (if source-controlled DB object exists) ==='
SELECT CASE
    WHEN to_regclass('inventory.v_inventory_alerts_ceo_for_package') IS NULL THEN 'VIEW_MISSING'
    ELSE pg_get_viewdef('inventory.v_inventory_alerts_ceo_for_package'::regclass, true)
END AS view_definition;

\echo '=== 7. INVENTORY DAILY SUMMARY VIEW DEFINITION ==='
SELECT CASE
    WHEN to_regclass('inventory.v_inventory_daily_summary') IS NULL THEN 'VIEW_MISSING'
    ELSE pg_get_viewdef('inventory.v_inventory_daily_summary'::regclass, true)
END AS view_definition;

\echo '=== 8. OPTIONAL DAILY TARGET TABLE ==='
SELECT to_regclass('ops.daily_sales_targets') AS daily_target_relation;
