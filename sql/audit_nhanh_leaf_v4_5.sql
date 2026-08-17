-- CEO Daily P&L v4.5 - Audit Nhanh leaf channels by exact label
-- Mirrors control_tower.v_scorecard_nhanh_daily_v2_12 and the v4.5 builder.
-- Run in database DuLieu after Nhanh ETL has completed.

WITH item AS (
    SELECT
        i.order_id,
        SUM(COALESCE(i.quantity,0) * COALESCE(i.price,0))::numeric AS gross_sales,
        SUM(CASE WHEN NOT COALESCE(i.is_gift,false)
                 THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS main_cogs,
        SUM(CASE WHEN COALESCE(i.is_gift,false)
                 THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS gift_cogs,
        SUM(COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0))::numeric AS cogs,
        SUM(COALESCE(i.quantity,0))::numeric AS qty
    FROM nhanh.order_items i
    GROUP BY i.order_id
), leaf AS (
    SELECT
        o.order_date::date AS report_date,
        BTRIM(COALESCE(o.label,'')) AS label,
        SUM(COALESCE(i.gross_sales,0))::numeric AS gross_sales,
        SUM(COALESCE(o.order_value,0))::numeric AS net_sales,
        COUNT(*)::bigint AS orders,
        COUNT(*) FILTER (
            WHERE BTRIM(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Đã hoàn','Khách hủy')
        )::bigint AS cancelled_orders,
        SUM(COALESCE(i.cogs,0))::numeric AS cogs,
        SUM(COALESCE(o.shipping_fee,0))::numeric AS shipping_cost,
        SUM(COALESCE(i.qty,0) * 2000)::numeric AS packaging_cost,
        SUM(CASE
                WHEN BTRIM(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Đã hoàn','Khách hủy') THEN 0
                ELSE GREATEST(COALESCE(o.order_value,0),0) * 0.15
            END)::numeric AS backoffice_cost
    FROM nhanh.orders o
    LEFT JOIN item i ON i.order_id=o.id
    WHERE o.order_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'
    GROUP BY o.order_date::date, BTRIM(COALESCE(o.label,''))
)
SELECT *
FROM leaf
ORDER BY report_date, label;

-- Hard audit: sales labels in DB must be one of the seven ETL-approved labels.
SELECT
    BTRIM(COALESCE(label,'')) AS unexpected_label,
    COUNT(*) AS orders,
    SUM(COALESCE(order_value,0)) AS net_sales
FROM nhanh.orders
WHERE order_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'
  AND BTRIM(COALESCE(label,'')) NOT IN (
      'FB - LPM Vietnam',
      'FB - LPM Vietnam 168',
      'FB -Blédina Brasses VN',
      'landing page',
      'Web - lepetitmarseillais.vn',
      'OA - LPM Vietnam',
      'INSTAGRAM'
  )
GROUP BY BTRIM(COALESCE(label,''))
ORDER BY orders DESC;

-- Ads labels that cannot be assigned to one of the seven sales leaves.
SELECT
    BTRIM(COALESCE(shop_label,'DEFAULT')) AS unallocated_ads_label,
    COUNT(*) AS rows,
    SUM(COALESCE(cost_vnd_vat, cost_vnd, 0)) AS ads_cost
FROM nhanh.ads_costs
WHERE ads_date BETWEEN DATE '2026-08-01' AND DATE '2026-08-31'
  AND BTRIM(COALESCE(shop_label,'DEFAULT')) NOT IN (
      'FB - LPM Vietnam',
      'FB - LPM Vietnam 168',
      'FB -Blédina Brasses VN',
      'landing page',
      'Web - lepetitmarseillais.vn',
      'OA - LPM Vietnam',
      'INSTAGRAM'
  )
GROUP BY BTRIM(COALESCE(shop_label,'DEFAULT'))
ORDER BY ads_cost DESC;
