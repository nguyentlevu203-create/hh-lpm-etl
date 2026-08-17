-- HH Marketplace Status Audit v4.5
-- Business rule:
--   ONLY normalized exact status "đã hủy" is cancelled.
--   Return/refund order classification is disabled; no return bucket is produced.
--   Every other status is normal/non-cancelled. Platform-funded voucher is ignored by the package.
--
-- Change the two dates below when auditing another period.

-- ============================================================
-- 1) SHOPEE - all raw statuses and exact classification
-- ============================================================
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
), s AS (
    SELECT
        o.order_date,
        COALESCE(NULLIF(btrim(o.status), ''), '<BLANK>') AS raw_status,
        lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy' AS exact_cancelled,
        COALESCE(o.is_cancelled,false) AS stored_is_cancelled,
        COALESCE(e.gross_sales,o.gross_sales,0)::numeric AS gross_sales
    FROM shopee.orders o
    LEFT JOIN shopee.order_financials_extra e
      ON e.external_order_id=o.external_order_id
     AND e.shop_label=o.shop_label
    CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT
    raw_status,
    exact_cancelled,
    stored_is_cancelled,
    COUNT(*) AS orders,
    SUM(gross_sales) AS gross_sales
FROM s
GROUP BY raw_status, exact_cancelled, stored_is_cancelled
ORDER BY exact_cancelled DESC, orders DESC, raw_status;

-- Shopee MUST return 0 mismatches after re-import.
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
)
SELECT
    COUNT(*) AS shopee_is_cancelled_mismatch
FROM shopee.orders o
CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2
  AND COALESCE(o.is_cancelled,false) <>
      (lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy');

-- Shopee daily control: Gross = non-cancel Gross + exact-cancel Gross.
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
), x AS (
    SELECT
        o.order_date,
        lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy' AS exact_cancelled,
        COALESCE(e.gross_sales,o.gross_sales,0)::numeric AS gross_sales
    FROM shopee.orders o
    LEFT JOIN shopee.order_financials_extra e
      ON e.external_order_id=o.external_order_id
     AND e.shop_label=o.shop_label
    CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT
    order_date,
    COUNT(*) AS orders,
    COUNT(*) FILTER (WHERE exact_cancelled) AS cancelled_orders,
    COUNT(*) FILTER (WHERE NOT exact_cancelled) AS non_cancelled_orders,
    SUM(gross_sales) AS gross_all_status,
    SUM(gross_sales) FILTER (WHERE exact_cancelled) AS gross_cancelled,
    SUM(gross_sales) FILTER (WHERE NOT exact_cancelled) AS gross_non_cancelled,
    SUM(gross_sales)
      - COALESCE(SUM(gross_sales) FILTER (WHERE exact_cancelled),0)
      - COALESCE(SUM(gross_sales) FILTER (WHERE NOT exact_cancelled),0) AS gross_reconciliation_gap
FROM x
GROUP BY order_date
ORDER BY order_date;

-- ============================================================
-- 2) TIKTOK - all raw statuses and exact classification
-- ============================================================
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
), s AS (
    SELECT
        o.order_date,
        COALESCE(NULLIF(btrim(o.order_status), ''), '<BLANK>') AS raw_status,
        lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy' AS exact_cancelled,
        COALESCE(o.is_cancelled,false) AS stored_is_cancelled,
        COALESCE(g.gross_all_order,o.gmv_before_cancel,o.fee_base_revenue,0)::numeric AS gross_sales
    FROM tiktok.orders_pnl o
    LEFT JOIN (
        SELECT external_order_id, shop_label,
               SUM(COALESCE(quantity,0)*COALESCE(price,0))::numeric AS gross_all_order
        FROM tiktok.order_items
        GROUP BY external_order_id, shop_label
    ) g
      ON g.external_order_id=o.external_order_id
     AND g.shop_label=o.shop_label
    CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT
    raw_status,
    exact_cancelled,
    stored_is_cancelled,
    COUNT(*) AS orders,
    SUM(gross_sales) AS gross_sales
FROM s
GROUP BY raw_status, exact_cancelled, stored_is_cancelled
ORDER BY exact_cancelled DESC, orders DESC, raw_status;

-- TikTok MUST return 0 mismatches after re-import.
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
)
SELECT
    COUNT(*) AS tiktok_is_cancelled_mismatch
FROM tiktok.orders_pnl o
CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2
  AND COALESCE(o.is_cancelled,false) <>
      (lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy');

-- TikTok daily control: Gross = non-cancel Gross + exact-cancel Gross.
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
), x AS (
    SELECT
        o.order_date,
        lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy' AS exact_cancelled,
        COALESCE(g.gross_all_order,o.gmv_before_cancel,o.fee_base_revenue,0)::numeric AS gross_sales
    FROM tiktok.orders_pnl o
    LEFT JOIN (
        SELECT external_order_id, shop_label,
               SUM(COALESCE(quantity,0)*COALESCE(price,0))::numeric AS gross_all_order
        FROM tiktok.order_items
        GROUP BY external_order_id, shop_label
    ) g
      ON g.external_order_id=o.external_order_id
     AND g.shop_label=o.shop_label
    CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT
    order_date,
    COUNT(*) AS orders,
    COUNT(*) FILTER (WHERE exact_cancelled) AS cancelled_orders,
    COUNT(*) FILTER (WHERE NOT exact_cancelled) AS non_cancelled_orders,
    SUM(gross_sales) AS gross_all_status,
    SUM(gross_sales) FILTER (WHERE exact_cancelled) AS gross_cancelled,
    SUM(gross_sales) FILTER (WHERE NOT exact_cancelled) AS gross_non_cancelled,
    SUM(gross_sales)
      - COALESCE(SUM(gross_sales) FILTER (WHERE exact_cancelled),0)
      - COALESCE(SUM(gross_sales) FILTER (WHERE NOT exact_cancelled),0) AS gross_reconciliation_gap
FROM x
GROUP BY order_date
ORDER BY order_date;

-- ============================================================
-- 3) Hard gate - both must be zero
-- ============================================================
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
), q AS (
    SELECT 'SHOPEE'::text AS channel,
           COUNT(*) FILTER (
               WHERE COALESCE(o.is_cancelled,false) <>
                     (lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy')
           )::bigint AS mismatch
    FROM shopee.orders o CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
    UNION ALL
    SELECT 'TIKTOK'::text,
           COUNT(*) FILTER (
               WHERE COALESCE(o.is_cancelled,false) <>
                     (lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy')
           )::bigint
    FROM tiktok.orders_pnl o CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT *, CASE WHEN mismatch=0 THEN 'PASS' ELSE 'FAIL - REIMPORT REQUIRED' END AS status
FROM q
ORDER BY channel;


-- ============================================================
-- 4) SHOPEE voucher semantic audit (read-only)
-- ============================================================
-- IMPORTANT: the legacy DB column name platform_voucher is misleading.
-- Repo ETL writes it from raw "Mã giảm giá của Shop" / "shop voucher".
-- v4.5 package therefore maps this amount to seller_voucher and never exposes
-- it as a platform-funded voucher.
WITH p AS (
    SELECT DATE '2026-08-01' AS d1, DATE '2026-08-13' AS d2
)
SELECT
    o.order_date,
    SUM(CASE WHEN lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) <> 'đã hủy'
             THEN COALESCE(e.seller_discount,0) ELSE 0 END)::numeric AS seller_discount,
    SUM(CASE WHEN lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) <> 'đã hủy'
             THEN COALESCE(e.platform_voucher,0) ELSE 0 END)::numeric AS seller_voucher_legacy_column
FROM shopee.orders o
LEFT JOIN shopee.order_financials_extra e
  ON e.external_order_id=o.external_order_id
 AND e.shop_label=o.shop_label
CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2
GROUP BY o.order_date
ORDER BY o.order_date;
