-- v4.7.4 audit: exact alias integrity + Shopee gift classification visibility.

-- 1) No exact active SKU alias may point to more than one product within a source.
SELECT
    source_system,
    dim.norm_identifier(identifier_value) AS identifier_norm,
    COUNT(DISTINCT product_id) AS product_count,
    ARRAY_AGG(DISTINCT product_id::text ORDER BY product_id::text) AS product_ids
FROM dim.product_identifier_map
WHERE is_active IS TRUE AND identifier_type = 'SKU'
GROUP BY source_system, dim.norm_identifier(identifier_value)
HAVING COUNT(DISTINCT product_id) > 1
ORDER BY source_system, identifier_norm;

-- 2) EAN aliases must also be unambiguous globally.
SELECT
    dim.norm_identifier(identifier_value) AS ean_norm,
    COUNT(DISTINCT product_id) AS product_count,
    ARRAY_AGG(DISTINCT product_id::text ORDER BY product_id::text) AS product_ids
FROM dim.product_identifier_map
WHERE is_active IS TRUE AND identifier_type = 'EAN'
GROUP BY dim.norm_identifier(identifier_value)
HAVING COUNT(DISTINCT product_id) > 1
ORDER BY ean_norm;

-- 3) Shopee gift classification rules written by the v4.7.4 ETL patch.
SELECT
    o.order_date::date AS report_date,
    COALESCE(i.raw_payload->>'gift_classification_rule', 'LEGACY_OR_UNKNOWN') AS gift_classification_rule,
    COUNT(*) AS item_rows,
    SUM(COALESCE(i.quantity,0)) AS qty,
    SUM(CASE WHEN COALESCE(i.is_gift,false) THEN COALESCE(i.quantity,0) ELSE 0 END) AS gift_qty,
    SUM(CASE WHEN NOT COALESCE(i.is_gift,false) THEN COALESCE(i.quantity,0) ELSE 0 END) AS sold_qty,
    SUM(CASE WHEN lower(COALESCE(i.raw_payload->>'gift_review_required','false')) IN ('true','1','yes') THEN 1 ELSE 0 END) AS review_rows
FROM shopee.orders o
JOIN shopee.order_items i ON i.order_id=o.id
WHERE o.order_date >= date_trunc('month', CURRENT_DATE)::date
GROUP BY o.order_date::date, COALESCE(i.raw_payload->>'gift_classification_rule', 'LEGACY_OR_UNKNOWN')
ORDER BY report_date, gift_classification_rule;

-- 4) COGS split must always reconcile to total item COGS for non-cancelled exact-status rows.
WITH x AS (
    SELECT
        o.order_date::date AS report_date,
        SUM(CASE WHEN NOT COALESCE(i.is_gift,false) THEN COALESCE(i.quantity,0)*COALESCE(i.unit_cogs,0) ELSE 0 END) AS sold_cogs,
        SUM(CASE WHEN COALESCE(i.is_gift,false) THEN COALESCE(i.quantity,0)*COALESCE(i.unit_cogs,0) ELSE 0 END) AS gift_cogs,
        SUM(COALESCE(i.quantity,0)*COALESCE(i.unit_cogs,0)) AS total_item_cogs
    FROM shopee.orders o
    JOIN shopee.order_items i ON i.order_id=o.id
    WHERE lower(regexp_replace(btrim(COALESCE(o.status,'')), '[[:space:]]+', ' ', 'g')) <> 'đã hủy'
    GROUP BY o.order_date::date
)
SELECT *, (sold_cogs + gift_cogs) - total_item_cogs AS reconciliation_delta
FROM x
WHERE ABS((sold_cogs + gift_cogs) - total_item_cogs) > 1
ORDER BY report_date;
