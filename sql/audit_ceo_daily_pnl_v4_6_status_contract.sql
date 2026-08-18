-- CEO Daily P&L v4.6 status-contract audit
-- Change date_from/date_to as needed.
-- Contract:
--   SHOPEE cancelled = exact normalized 'Đã hủy'
--   TIKTOK cancelled = exact normalized 'Canceled'
--   NHANH cancelled  = exact trimmed one of:
--     'Đã hủy', 'Hệ Thống hủy', 'Khách hủy', 'HVC hủy'
--   NHANH return statuses are NOT cancelled:
--     'Đang hoàn', 'Xác nhận hoàn', 'Đã hoàn'
--   NHANH 'Thất bại' is NOT cancelled.

\set ON_ERROR_STOP on

WITH p AS (
    SELECT DATE '2026-08-01' AS date_from, DATE '2026-08-14' AS date_to
)
SELECT 'AUDIT_RANGE' AS check_name, date_from::text AS value_1, date_to::text AS value_2
FROM p;

-- 1) SHOPEE raw statuses and stored flag.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2), x AS (
    SELECT
        COALESCE(NULLIF(btrim(o.status),''), '<BLANK>') AS raw_status,
        lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy' AS expected_cancelled,
        COALESCE(o.is_cancelled,false) AS stored_is_cancelled
    FROM shopee.orders o CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT 'SHOPEE_STATUS' AS check_name, raw_status, expected_cancelled, stored_is_cancelled, COUNT(*) AS orders
FROM x GROUP BY raw_status, expected_cancelled, stored_is_cancelled
ORDER BY orders DESC, raw_status;

-- Shopee hard gate must be 0 after re-import.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2)
SELECT 'SHOPEE_FLAG_MISMATCH' AS check_name,
       COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled,false) <>
         (lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy')) AS mismatch
FROM shopee.orders o CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2;

-- 2) TIKTOK raw statuses and stored flag.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2), x AS (
    SELECT
        COALESCE(NULLIF(btrim(o.order_status),''), '<BLANK>') AS raw_status,
        lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'canceled' AS expected_cancelled,
        COALESCE(o.is_cancelled,false) AS stored_is_cancelled
    FROM tiktok.orders_pnl o CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT 'TIKTOK_STATUS' AS check_name, raw_status, expected_cancelled, stored_is_cancelled, COUNT(*) AS orders
FROM x GROUP BY raw_status, expected_cancelled, stored_is_cancelled
ORDER BY orders DESC, raw_status;

-- TikTok hard gate must be 0 after re-import.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2)
SELECT 'TIKTOK_FLAG_MISMATCH' AS check_name,
       COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled,false) <>
         (lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'canceled')) AS mismatch
FROM tiktok.orders_pnl o CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2;

-- Explicit semantic gate: every raw Canceled must be stored true.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2)
SELECT 'TIKTOK_CANCELED_FALSE' AS check_name,
       COUNT(*) AS mismatch
FROM tiktok.orders_pnl o CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2
  AND lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'canceled'
  AND NOT COALESCE(o.is_cancelled,false);

-- 3) NHANH raw statuses: separate cancel vs return vs failure.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2), x AS (
    SELECT
        COALESCE(NULLIF(btrim(o.status),''), '<BLANK>') AS raw_status,
        btrim(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Khách hủy','HVC hủy') AS expected_cancelled,
        btrim(COALESCE(o.status,'')) IN ('Đang hoàn','Xác nhận hoàn','Đã hoàn') AS return_status_not_cancelled,
        btrim(COALESCE(o.status,'')) = 'Thất bại' AS failed_status_not_cancelled,
        COALESCE(o.is_cancelled,false) AS stored_is_cancelled
    FROM nhanh.orders o CROSS JOIN p
    WHERE o.order_date BETWEEN p.d1 AND p.d2
)
SELECT 'NHANH_STATUS' AS check_name, raw_status, expected_cancelled,
       return_status_not_cancelled, failed_status_not_cancelled,
       stored_is_cancelled, COUNT(*) AS orders
FROM x
GROUP BY raw_status, expected_cancelled, return_status_not_cancelled, failed_status_not_cancelled, stored_is_cancelled
ORDER BY orders DESC, raw_status;

-- Nhanh DB mismatch is informational for v4.6 package because the package
-- recomputes cancellation directly from raw status. Re-importing Nhanh will clean it.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2)
SELECT 'NHANH_FLAG_MISMATCH_INFORMATIONAL' AS check_name,
       COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled,false) <>
         (btrim(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Khách hủy','HVC hủy'))) AS mismatch
FROM nhanh.orders o CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2;

-- Return statuses must never overlap with the v4.6 cancel whitelist.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2)
SELECT 'NHANH_RETURN_COUNT_NOT_CANCELLED' AS check_name,
       COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) IN ('Đang hoàn','Xác nhận hoàn','Đã hoàn')) AS return_status_orders,
       COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) = 'Thất bại') AS failed_status_orders,
       COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Khách hủy','HVC hủy')) AS cancelled_orders
FROM nhanh.orders o CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2;

-- 4) Nhanh daily parent order counts directly from raw status.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2)
SELECT
    'NHANH_DAILY_PARENT_COUNTS' AS check_name,
    o.order_date,
    COUNT(*) AS orders,
    COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Khách hủy','HVC hủy')) AS cancelled_orders,
    COUNT(*) FILTER (WHERE NOT (btrim(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Khách hủy','HVC hủy'))) AS non_cancelled_orders,
    COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) = 'Thành công') AS successful_orders_exact,
    COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) IN ('Đang hoàn','Xác nhận hoàn','Đã hoàn')) AS return_status_orders,
    COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) = 'Thất bại') AS failed_status_orders
FROM nhanh.orders o CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2
GROUP BY o.order_date
ORDER BY o.order_date;

-- 5) Cancel-like Nhanh text outside whitelist: should be reviewed, not auto-classified.
WITH p AS (SELECT DATE '2026-08-01' AS d1, DATE '2026-08-14' AS d2)
SELECT 'NHANH_UNMAPPED_CANCEL_LIKE' AS check_name,
       btrim(COALESCE(o.status,'')) AS raw_status,
       COUNT(*) AS orders
FROM nhanh.orders o CROSS JOIN p
WHERE o.order_date BETWEEN p.d1 AND p.d2
  AND lower(btrim(COALESCE(o.status,''))) LIKE '%hủy%'
  AND btrim(COALESCE(o.status,'')) NOT IN ('Đã hủy','Hệ Thống hủy','Khách hủy','HVC hủy')
GROUP BY btrim(COALESCE(o.status,''))
ORDER BY orders DESC, raw_status;
