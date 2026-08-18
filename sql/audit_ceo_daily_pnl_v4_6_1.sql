-- CEO Daily P&L v4.6.1 audit
-- Default report date below is 2026-08-14. Change only the report_date CTE for another run.
-- Read-only: no UPDATE/DELETE/ALTER.

WITH params AS (
    SELECT DATE '2026-08-14' AS report_date
), periods AS (
    SELECT date_trunc('month', report_date)::date AS start_date, report_date AS end_date, 'current_mtd'::text AS label FROM params
    UNION ALL
    SELECT date_trunc('month', report_date - INTERVAL '1 month')::date,
           (report_date - INTERVAL '1 month')::date,
           'previous_mtd_same_period' FROM params
    UNION ALL
    SELECT (report_date - EXTRACT(ISODOW FROM report_date)::int + 1 - 7)::date,
           (report_date - 7)::date,
           'previous_wtd_same_days' FROM params
    UNION ALL
    SELECT (report_date - 1)::date, (report_date - 1)::date, 'comparison_date' FROM params
)
SELECT * FROM periods ORDER BY start_date, end_date;

-- 1) SHOPEE stored flag hard gate. Expected mismatch = 0.
WITH params AS (SELECT DATE '2026-08-14' AS report_date), periods AS (
    SELECT date_trunc('month', report_date)::date s, report_date e FROM params
    UNION SELECT date_trunc('month', report_date - INTERVAL '1 month')::date, (report_date - INTERVAL '1 month')::date FROM params
    UNION SELECT (report_date - EXTRACT(ISODOW FROM report_date)::int + 1 - 7)::date, (report_date - 7)::date FROM params
    UNION SELECT (report_date - 1)::date, (report_date - 1)::date FROM params
)
SELECT 'SHOPEE_FLAG_MISMATCH' AS check_name, COUNT(*) AS mismatch
FROM shopee.orders o
WHERE EXISTS (SELECT 1 FROM periods p WHERE o.order_date BETWEEN p.s AND p.e)
  AND COALESCE(o.is_cancelled,false) <>
      (lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy');

-- 2) TIKTOK stored flag hard gate. Expected mismatch = 0.
WITH params AS (SELECT DATE '2026-08-14' AS report_date), periods AS (
    SELECT date_trunc('month', report_date)::date s, report_date e FROM params
    UNION SELECT date_trunc('month', report_date - INTERVAL '1 month')::date, (report_date - INTERVAL '1 month')::date FROM params
    UNION SELECT (report_date - EXTRACT(ISODOW FROM report_date)::int + 1 - 7)::date, (report_date - 7)::date FROM params
    UNION SELECT (report_date - 1)::date, (report_date - 1)::date FROM params
)
SELECT 'TIKTOK_FLAG_MISMATCH' AS check_name, COUNT(*) AS mismatch
FROM tiktok.orders_pnl o
WHERE EXISTS (SELECT 1 FROM periods p WHERE o.order_date BETWEEN p.s AND p.e)
  AND COALESCE(o.is_cancelled,false) <>
      (lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) = 'canceled');

-- 3) NHANH normalized-exact classification audit.
-- Note: DB flag mismatch is WARNING only for CEO package v4.6.1 because reporting recomputes from raw status.
WITH params AS (SELECT DATE '2026-08-14' AS report_date), periods AS (
    SELECT date_trunc('month', report_date)::date s, report_date e FROM params
    UNION SELECT date_trunc('month', report_date - INTERVAL '1 month')::date, (report_date - INTERVAL '1 month')::date FROM params
    UNION SELECT (report_date - EXTRACT(ISODOW FROM report_date)::int + 1 - 7)::date, (report_date - 7)::date FROM params
    UNION SELECT (report_date - 1)::date, (report_date - 1)::date FROM params
), src AS (
    SELECT
        o.status,
        lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) AS status_norm,
        COALESCE(o.is_cancelled,false) AS stored_is_cancelled
    FROM nhanh.orders o
    WHERE EXISTS (SELECT 1 FROM periods p WHERE o.order_date BETWEEN p.s AND p.e)
)
SELECT
    COALESCE(NULLIF(btrim(status),''), '<BLANK>') AS raw_status,
    status_norm,
    status_norm IN ('đã hủy','hệ thống hủy','khách hủy','hvc hủy') AS classified_cancelled,
    status_norm IN ('đang hoàn','xác nhận hoàn','đã hoàn') AS classified_return,
    status_norm = 'thất bại' AS classified_failed,
    stored_is_cancelled,
    COUNT(*) AS orders
FROM src
GROUP BY 1,2,3,4,5,6
ORDER BY orders DESC, raw_status;

-- 4) NHANH legacy DB flag mismatch breakdown.
WITH params AS (SELECT DATE '2026-08-14' AS report_date), periods AS (
    SELECT date_trunc('month', report_date)::date s, report_date e FROM params
    UNION SELECT date_trunc('month', report_date - INTERVAL '1 month')::date, (report_date - INTERVAL '1 month')::date FROM params
    UNION SELECT (report_date - EXTRACT(ISODOW FROM report_date)::int + 1 - 7)::date, (report_date - 7)::date FROM params
    UNION SELECT (report_date - 1)::date, (report_date - 1)::date FROM params
), src AS (
    SELECT
        COALESCE(o.is_cancelled,false) AS stored_is_cancelled,
        lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) IN
          ('đã hủy','hệ thống hủy','khách hủy','hvc hủy') AS expected_cancelled
    FROM nhanh.orders o
    WHERE EXISTS (SELECT 1 FROM periods p WHERE o.order_date BETWEEN p.s AND p.e)
)
SELECT
    COUNT(*) FILTER (WHERE stored_is_cancelled <> expected_cancelled) AS mismatch_total,
    COUNT(*) FILTER (WHERE expected_cancelled AND NOT stored_is_cancelled) AS false_negative_cancel,
    COUNT(*) FILTER (WHERE NOT expected_cancelled AND stored_is_cancelled) AS false_positive_cancel
FROM src;

-- 5) Genuine cancel-like Nhanh tokens outside approved whitelist.
-- Expected result: NO ROWS. "Đang chuyển" must not appear.
WITH params AS (SELECT DATE '2026-08-14' AS report_date), periods AS (
    SELECT date_trunc('month', report_date)::date s, report_date e FROM params
    UNION SELECT date_trunc('month', report_date - INTERVAL '1 month')::date, (report_date - INTERVAL '1 month')::date FROM params
    UNION SELECT (report_date - EXTRACT(ISODOW FROM report_date)::int + 1 - 7)::date, (report_date - 7)::date FROM params
    UNION SELECT (report_date - 1)::date, (report_date - 1)::date FROM params
), src AS (
    SELECT
        COALESCE(NULLIF(btrim(o.status),''), '<BLANK>') AS raw_status,
        lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) AS status_norm
    FROM nhanh.orders o
    WHERE EXISTS (SELECT 1 FROM periods p WHERE o.order_date BETWEEN p.s AND p.e)
)
SELECT raw_status, status_norm, COUNT(*) AS orders
FROM src
WHERE status_norm NOT IN ('đã hủy','hệ thống hủy','khách hủy','hvc hủy')
  AND status_norm ~ '(^|[^[:alpha:]])(hủy|huỷ|huy|cancel|canceled|cancelled)([^[:alpha:]]|$)'
GROUP BY raw_status, status_norm
ORDER BY orders DESC, raw_status;
