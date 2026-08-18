-- CEO Daily P&L v4.7.2 source audit
-- Usage:
-- psql -v report_date="2026-08-17" -f sql/audit_ceo_daily_pnl_v4_7_2.sql

\echo '=== 1. GT/MT ALL CUSTOMERS MTD ==='
WITH p AS (
  SELECT :'report_date'::date AS report_date,
         date_trunc('month', :'report_date'::date)::date AS month_start
), base AS (
  SELECT
    upper(btrim(s.channel_group)) AS channel,
    CASE
      WHEN NULLIF(btrim(COALESCE(s.customer_code,'')), '') IS NOT NULL
      THEN 'CODE:' || lower(btrim(COALESCE(s.customer_code,'')))
      ELSE 'NAME:' || lower(regexp_replace(btrim(COALESCE(s.customer_name,'')), '[[:space:]]+', ' ', 'g'))
    END AS customer_key,
    s.document_no,
    COALESCE(s.net_revenue,0) AS net_revenue
  FROM mtgt.sales_lines s
  CROSS JOIN p
  WHERE s.sale_date BETWEEN p.month_start AND p.report_date
    AND upper(btrim(s.channel_group)) IN ('GT','MT')
    AND COALESCE(s.line_type,'') <> 'CVC'
)
SELECT channel,
       COUNT(DISTINCT customer_key) AS customer_count,
       COUNT(DISTINCT document_no) AS documents_mtd,
       SUM(net_revenue)::numeric AS net_sales_mtd
FROM base
GROUP BY channel
ORDER BY channel;

\echo '=== 2. INVENTORY SNAPSHOT RESOLVED <= REPORT DATE ==='
SELECT MAX(snapshot_date)::date AS resolved_snapshot_date
FROM inventory.stock_lot_snapshots
WHERE snapshot_date <= :'report_date'::date;

\echo '=== 3. DATE/EXPIRY ALERT ROWS FOR SHEET 06 ==='
WITH snap AS (
  SELECT MAX(snapshot_date)::date AS snapshot_date
  FROM inventory.stock_lot_snapshots
  WHERE snapshot_date <= :'report_date'::date
)
SELECT priority, alert_type, sku_code, ean, product_name,
       total_stock, affected_qty, affected_pct, earliest_expiry_date,
       reason, suggested_action
FROM inventory.v_inventory_alerts_ceo_for_package a
JOIN snap s ON a.snapshot_date = s.snapshot_date
WHERE a.earliest_expiry_date IS NOT NULL
ORDER BY priority, earliest_expiry_date, product_name;
