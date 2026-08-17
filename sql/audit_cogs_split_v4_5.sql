-- CEO Daily P&L v4.5 - audit sold COGS vs gift COGS
-- Hard gate expectation: gap = 0 (or <= 1 VND after package rounding).
-- Rules:
--   NHANH  : nhanh.order_items.is_gift is the source-of-truth flag.
--   SHOPEE : shopee.order_items.is_gift is the source-of-truth flag; cancelled rows already carry zero unit_cogs under ETL rule.
--   TIKTOK : item price = 0 => gift; item price <> 0 => sold; exact normalized status 'Đã hủy' => zero COGS.

WITH params AS (
    SELECT DATE '2026-08-01' AS date_from, DATE '2026-08-13' AS date_to
),
nhanh AS (
    SELECT
        o.order_date::date AS report_date,
        'NHANH'::text AS channel,
        SUM(CASE WHEN NOT COALESCE(i.is_gift,false)
                 THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS sold_cogs,
        SUM(CASE WHEN COALESCE(i.is_gift,false)
                 THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS gift_cogs,
        SUM(COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0))::numeric AS item_cogs_total
    FROM nhanh.orders o
    LEFT JOIN nhanh.order_items i ON i.order_id=o.id
    CROSS JOIN params p
    WHERE o.order_date BETWEEN p.date_from AND p.date_to
    GROUP BY o.order_date::date
),
shopee AS (
    SELECT
        o.order_date::date AS report_date,
        'SHOPEE'::text AS channel,
        SUM(CASE WHEN NOT COALESCE(i.is_gift,false)
                 THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS sold_cogs,
        SUM(CASE WHEN COALESCE(i.is_gift,false)
                 THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS gift_cogs,
        SUM(COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0))::numeric AS item_cogs_total
    FROM shopee.orders o
    LEFT JOIN shopee.order_items i ON i.order_id=o.id
    CROSS JOIN params p
    WHERE o.order_date BETWEEN p.date_from AND p.date_to
    GROUP BY o.order_date::date
),
tiktok AS (
    SELECT
        o.order_date::date AS report_date,
        'TIKTOK'::text AS channel,
        SUM(CASE
                WHEN lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) <> 'đã hủy'
                 AND COALESCE(i.price,0) <> 0
                THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0)
                ELSE 0
            END)::numeric AS sold_cogs,
        SUM(CASE
                WHEN lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) <> 'đã hủy'
                 AND COALESCE(i.price,0) = 0
                THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0)
                ELSE 0
            END)::numeric AS gift_cogs,
        SUM(CASE
                WHEN lower(regexp_replace(btrim(COALESCE(o.order_status,'')), '[[:space:]]+', ' ', 'g')) <> 'đã hủy'
                THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0)
                ELSE 0
            END)::numeric AS item_cogs_total
    FROM tiktok.orders_pnl o
    LEFT JOIN tiktok.order_items i
      ON i.external_order_id=o.external_order_id
     AND i.shop_label=o.shop_label
    CROSS JOIN params p
    WHERE o.order_date BETWEEN p.date_from AND p.date_to
    GROUP BY o.order_date::date
),
split AS (
    SELECT * FROM nhanh
    UNION ALL SELECT * FROM shopee
    UNION ALL SELECT * FROM tiktok
),
scorecard AS (
    SELECT report_date::date, upper(channel)::text AS channel, COALESCE(cogs,0)::numeric AS scorecard_cogs
    FROM control_tower.v_scorecard_by_channel_daily_for_package
    CROSS JOIN params p
    WHERE report_date BETWEEN p.date_from AND p.date_to
      AND upper(channel) IN ('NHANH','SHOPEE','TIKTOK')
)
SELECT
    s.report_date,
    s.channel,
    s.sold_cogs,
    s.gift_cogs,
    s.item_cogs_total,
    COALESCE(c.scorecard_cogs,0) AS scorecard_cogs,
    (s.sold_cogs + s.gift_cogs - s.item_cogs_total) AS split_internal_gap,
    (COALESCE(c.scorecard_cogs,0) - s.sold_cogs - s.gift_cogs) AS scorecard_split_gap,
    CASE
        WHEN ABS(s.sold_cogs + s.gift_cogs - s.item_cogs_total) <= 1
         AND ABS(COALESCE(c.scorecard_cogs,0) - s.sold_cogs - s.gift_cogs) <= 1
        THEN 'PASS' ELSE 'FAIL'
    END AS status
FROM split s
LEFT JOIN scorecard c USING (report_date, channel)
ORDER BY s.report_date, s.channel;
