from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import RealDictCursor
from api.database import get_db_connection

router = APIRouter(prefix="/api/v1/ceo", tags=["CEO - Báo Cáo Tổng Hợp"])

CEO_MASTER_SQL = """
WITH nhanh_item AS (
    SELECT order_id, SUM(COALESCE(quantity,0) * COALESCE(unit_cogs,0)) AS cogs, SUM(COALESCE(quantity,0)) AS qty
    FROM nhanh.order_items GROUP BY order_id
), nhanh_order AS (
    SELECT
        o.order_date,
        COALESCE(o.label, 'DEFAULT') AS shop_label,
        COUNT(*) AS total_orders,
        COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled, false) OR o.status IN ('Đã hoàn','Da hoan')) AS cancelled_orders,
        COUNT(*) FILTER (WHERE NOT (COALESCE(o.is_cancelled, false) OR o.status IN ('Đã hoàn','Da hoan'))) AS success_orders,
        SUM(COALESCE(o.order_value,0)) AS revenue,
        SUM(COALESCE(i.cogs,0)) AS cogs,
        0::NUMERIC AS platform_fees,
        SUM(COALESCE(o.shipping_fee,0)) AS shipping_fee,
        SUM(GREATEST(COALESCE(o.order_value,0),0) * 0.15) AS backoffice_fee,
        0::NUMERIC AS booking_fee,
        SUM(COALESCE(i.qty,0) * 2000) AS packaging_cost,
        0::NUMERIC AS live_cost,
        0::NUMERIC AS payout_estimated
    FROM nhanh.orders o
    LEFT JOIN nhanh_item i ON i.order_id = o.id
    WHERE o.order_date BETWEEN %s::date AND %s::date
    GROUP BY o.order_date, COALESCE(o.label, 'DEFAULT')
), nhanh_ads AS (
    SELECT ads_date AS order_date, shop_label, SUM(COALESCE(cost_vnd_vat,0)) AS ads_cost
    FROM nhanh.ads_costs
    WHERE ads_date BETWEEN %s::date AND %s::date
    GROUP BY ads_date, shop_label
), nhanh_daily AS (
    SELECT
        COALESCE(o.order_date, a.order_date) AS order_date,
        'NHANH'::TEXT AS source_system,
        COALESCE(o.shop_label, a.shop_label) AS shop_label,
        COALESCE(o.total_orders,0) AS total_orders,
        COALESCE(o.cancelled_orders,0) AS cancelled_orders,
        COALESCE(o.success_orders,0) AS success_orders,
        COALESCE(o.revenue,0) AS revenue,
        COALESCE(o.cogs,0) AS cogs,
        COALESCE(o.platform_fees,0) AS platform_fees,
        COALESCE(o.shipping_fee,0) AS shipping_fee,
        COALESCE(o.backoffice_fee,0) AS backoffice_fee,
        COALESCE(o.booking_fee,0) AS booking_fee,
        COALESCE(o.packaging_cost,0) AS packaging_cost,
        COALESCE(a.ads_cost,0) AS ads_cost,
        COALESCE(o.live_cost,0) AS live_cost,
        COALESCE(o.payout_estimated,0) AS payout_estimated,
        COALESCE(o.revenue,0) - COALESCE(o.cogs,0) - COALESCE(o.shipping_fee,0) - COALESCE(o.backoffice_fee,0) - COALESCE(o.packaging_cost,0) - COALESCE(a.ads_cost,0) AS profit
    FROM nhanh_order o
    FULL OUTER JOIN nhanh_ads a ON a.order_date = o.order_date AND a.shop_label = o.shop_label
), shopee_item AS (
    SELECT oi.order_id,
           SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0 ELSE COALESCE(oi.quantity,0) * COALESCE(oi.unit_cogs,0) END) AS cogs,
           SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0 ELSE COALESCE(oi.quantity,0) END) AS qty
    FROM shopee.order_items oi
    JOIN shopee.orders o ON o.id = oi.order_id
    GROUP BY oi.order_id
), shopee_order AS (
    SELECT
        o.order_date,
        o.shop_label,
        COUNT(*) AS total_orders,
        COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled,false)) AS cancelled_orders,
        COUNT(*) FILTER (WHERE NOT COALESCE(o.is_cancelled,false)) AS success_orders,
        SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0 ELSE COALESCE(o.seller_revenue,0) END) AS revenue,
        SUM(COALESCE(i.cogs,0)) AS cogs,
        SUM(COALESCE(o.fixed_fee,0) + COALESCE(o.service_fee,0) + COALESCE(o.payment_fee,0) + COALESCE(o.affiliate_amount,0)) AS platform_fees,
        SUM(COALESCE(o.return_fee,0)) AS shipping_fee,
        GREATEST(SUM(CASE WHEN COALESCE(o.payout_actual,0) <> 0 THEN COALESCE(o.payout_actual,0) ELSE COALESCE(o.est_payout,0) END),0) * 0.15 AS backoffice_fee,
        0::NUMERIC AS booking_fee,
        SUM(COALESCE(i.qty,0) * 2000) AS packaging_cost,
        0::NUMERIC AS live_cost,
        SUM(CASE WHEN COALESCE(o.payout_actual,0) <> 0 THEN COALESCE(o.payout_actual,0) ELSE COALESCE(o.est_payout,0) END) AS payout_estimated
    FROM shopee.orders o
    LEFT JOIN shopee_item i ON i.order_id = o.id
    WHERE o.order_date BETWEEN %s::date AND %s::date
    GROUP BY o.order_date, o.shop_label
), shopee_ads AS (
    SELECT ads_date AS order_date, shop_label, SUM(COALESCE(cost_vnd_vat,0)) AS ads_cost
    FROM shopee.ads_costs
    WHERE ads_date BETWEEN %s::date AND %s::date
    GROUP BY ads_date, shop_label
), shopee_daily AS (
    SELECT
        COALESCE(o.order_date, a.order_date) AS order_date,
        'SHOPEE'::TEXT AS source_system,
        COALESCE(o.shop_label, a.shop_label) AS shop_label,
        COALESCE(o.total_orders,0) AS total_orders,
        COALESCE(o.cancelled_orders,0) AS cancelled_orders,
        COALESCE(o.success_orders,0) AS success_orders,
        COALESCE(o.revenue,0) AS revenue,
        COALESCE(o.cogs,0) AS cogs,
        COALESCE(o.platform_fees,0) AS platform_fees,
        COALESCE(o.shipping_fee,0) AS shipping_fee,
        COALESCE(o.backoffice_fee,0) AS backoffice_fee,
        COALESCE(o.booking_fee,0) AS booking_fee,
        COALESCE(o.packaging_cost,0) AS packaging_cost,
        COALESCE(a.ads_cost,0) AS ads_cost,
        COALESCE(o.live_cost,0) AS live_cost,
        COALESCE(o.payout_estimated,0) AS payout_estimated,
        COALESCE(o.revenue,0) - COALESCE(o.cogs,0) - COALESCE(o.shipping_fee,0) - COALESCE(o.platform_fees,0) - COALESCE(o.backoffice_fee,0) - COALESCE(o.packaging_cost,0) - COALESCE(a.ads_cost,0) AS profit
    FROM shopee_order o
    FULL OUTER JOIN shopee_ads a ON a.order_date = o.order_date AND a.shop_label = o.shop_label
), tiktok_order AS (
    SELECT
        o.order_date,
        o.shop_label,
        COUNT(*) AS total_orders,
        COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled,false)) AS cancelled_orders,
        COUNT(*) FILTER (WHERE NOT COALESCE(o.is_cancelled,false)) AS success_orders,
        SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0 ELSE COALESCE(o.fee_base_revenue,0) END) AS revenue,
        SUM(COALESCE(o.cogs_amount,0)) AS cogs,
        SUM(COALESCE(o.fixed_fee,0) + COALESCE(o.payment_fee,0) + COALESCE(o.vxp_fee,0) + COALESCE(o.infrastructure_fee,0)) AS platform_fees,
        SUM(COALESCE(o.return_shipping_fee,0)) AS shipping_fee,
        SUM(COALESCE(o.backoffice_cost,0)) AS backoffice_fee,
        SUM(COALESCE(o.booking_fee,0)) AS booking_fee,
        SUM(COALESCE(o.packaging_cost,0)) AS packaging_cost,
        SUM(CASE WHEN COALESCE(o.settlement_paid,0) + COALESCE(o.settlement_unpaid,0) <> 0 THEN COALESCE(o.settlement_paid,0) + COALESCE(o.settlement_unpaid,0) ELSE COALESCE(o.estimated_payout,0) END) AS payout_estimated
    FROM tiktok.orders_pnl o
    WHERE o.order_date BETWEEN %s::date AND %s::date
    GROUP BY o.order_date, o.shop_label
), tiktok_ads AS (
    SELECT ads_date AS order_date, shop_label, SUM(COALESCE(cost_vnd_vat,0)) AS ads_cost
    FROM tiktok.ads_costs
    WHERE ads_date BETWEEN %s::date AND %s::date
    GROUP BY ads_date, shop_label
), tiktok_live AS (
    SELECT live_date AS order_date, shop_label, SUM(COALESCE(in_house_cost,0)) AS in_house_cost, SUM(COALESCE(teaser_cost,0)) AS teaser_cost
    FROM tiktok.live_costs
    WHERE live_date BETWEEN %s::date AND %s::date
    GROUP BY live_date, shop_label
), tiktok_daily AS (
    SELECT
        COALESCE(o.order_date, a.order_date, l.order_date) AS order_date,
        'TIKTOK'::TEXT AS source_system,
        COALESCE(o.shop_label, a.shop_label, l.shop_label) AS shop_label,
        COALESCE(o.total_orders,0) AS total_orders,
        COALESCE(o.cancelled_orders,0) AS cancelled_orders,
        COALESCE(o.success_orders,0) AS success_orders,
        COALESCE(o.revenue,0) AS revenue,
        COALESCE(o.cogs,0) AS cogs,
        COALESCE(o.platform_fees,0) AS platform_fees,
        COALESCE(o.shipping_fee,0) AS shipping_fee,
        COALESCE(o.backoffice_fee,0) AS backoffice_fee,
        COALESCE(o.booking_fee,0) AS booking_fee,
        COALESCE(o.packaging_cost,0) AS packaging_cost,
        COALESCE(a.ads_cost,0) + COALESCE(l.teaser_cost,0) AS ads_cost,
        COALESCE(l.in_house_cost,0) AS live_cost,
        COALESCE(o.payout_estimated,0) AS payout_estimated,
        COALESCE(o.revenue,0) - COALESCE(o.cogs,0) - COALESCE(o.shipping_fee,0) - COALESCE(o.platform_fees,0) - COALESCE(o.backoffice_fee,0) - COALESCE(o.booking_fee,0) - COALESCE(o.packaging_cost,0) - (COALESCE(a.ads_cost,0) + COALESCE(l.teaser_cost,0)) - COALESCE(l.in_house_cost,0) AS profit
    FROM tiktok_order o
    FULL OUTER JOIN tiktok_ads a ON a.order_date = o.order_date AND a.shop_label = o.shop_label
    FULL OUTER JOIN tiktok_live l ON l.order_date = COALESCE(o.order_date, a.order_date) AND l.shop_label = COALESCE(o.shop_label, a.shop_label)
), mtgt_daily AS (
    SELECT
        s.sale_date AS order_date,
        'MTGT'::TEXT AS source_system,
        s.channel_group::TEXT AS shop_label,
        COUNT(DISTINCT COALESCE(s.document_no, s.id::TEXT)) AS total_orders,
        0::BIGINT AS cancelled_orders,
        COUNT(DISTINCT COALESCE(s.document_no, s.id::TEXT)) AS success_orders,
        SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END) AS revenue,
        SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.quantity,0) * COALESCE(s.unit_cogs,0) ELSE 0 END) AS cogs,
        0::NUMERIC AS platform_fees,
        SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END) * 0.02 AS shipping_fee,
        SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END) * 0.15 AS backoffice_fee,
        0::NUMERIC AS booking_fee,
        0::NUMERIC AS packaging_cost,
        0::NUMERIC AS ads_cost,
        0::NUMERIC AS live_cost,
        0::NUMERIC AS payout_estimated,
        SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END)
        - SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.quantity,0) * COALESCE(s.unit_cogs,0) ELSE 0 END)
        - SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END) * 0.02
        - SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END) * 0.15 AS profit
    FROM mtgt.sales_lines s
    WHERE s.sale_date BETWEEN %s::date AND %s::date
    GROUP BY s.sale_date, s.channel_group
), all_daily AS (
    SELECT * FROM nhanh_daily
    UNION ALL SELECT * FROM shopee_daily
    UNION ALL SELECT * FROM tiktok_daily
    UNION ALL SELECT * FROM mtgt_daily
)
SELECT
    source_system,
    SUM(total_orders)::BIGINT AS orders,
    SUM(success_orders)::BIGINT AS success_orders,
    SUM(cancelled_orders)::BIGINT AS cancelled_orders,
    SUM(revenue)::NUMERIC(18,2) AS revenue,
    SUM(cogs)::NUMERIC(18,2) AS cogs,
    SUM(platform_fees)::NUMERIC(18,2) AS platform_fees,
    SUM(shipping_fee)::NUMERIC(18,2) AS shipping_fee,
    SUM(backoffice_fee)::NUMERIC(18,2) AS backoffice_fee,
    SUM(booking_fee)::NUMERIC(18,2) AS booking_fee,
    SUM(packaging_cost)::NUMERIC(18,2) AS packaging_cost,
    SUM(ads_cost)::NUMERIC(18,2) AS ads_cost,
    SUM(live_cost)::NUMERIC(18,2) AS live_cost,
    SUM(payout_estimated)::NUMERIC(18,2) AS payout_estimated,
    SUM(profit)::NUMERIC(18,2) AS profit,
    ROUND(SUM(profit) / NULLIF(SUM(revenue),0) * 100, 2) AS profit_margin_pct
FROM all_daily
GROUP BY source_system
ORDER BY source_system
"""

@router.get("/master-overview")
def get_ceo_master_overview(
    tu_ngay: str = Query(..., description="Từ ngày (YYYY-MM-DD)"),
    den_ngay: str = Query(..., description="Đến ngày (YYYY-MM-DD)"),
):
    conn = None
    cur = None
    try:
        conn = get_db_connection()
        cur = conn.cursor(cursor_factory=RealDictCursor)
        params = (
            tu_ngay, den_ngay, tu_ngay, den_ngay,
            tu_ngay, den_ngay, tu_ngay, den_ngay,
            tu_ngay, den_ngay, tu_ngay, den_ngay, tu_ngay, den_ngay,
            tu_ngay, den_ngay,
        )
        cur.execute(CEO_MASTER_SQL, params)
        channels = cur.fetchall()
        total = {
            "orders": sum(int(r["orders"] or 0) for r in channels),
            "success_orders": sum(int(r["success_orders"] or 0) for r in channels),
            "cancelled_orders": sum(int(r["cancelled_orders"] or 0) for r in channels),
            "revenue": sum(float(r["revenue"] or 0) for r in channels),
            "cogs": sum(float(r["cogs"] or 0) for r in channels),
            "platform_fees": sum(float(r["platform_fees"] or 0) for r in channels),
            "shipping_fee": sum(float(r["shipping_fee"] or 0) for r in channels),
            "backoffice_fee": sum(float(r["backoffice_fee"] or 0) for r in channels),
            "booking_fee": sum(float(r["booking_fee"] or 0) for r in channels),
            "packaging_cost": sum(float(r["packaging_cost"] or 0) for r in channels),
            "ads_cost": sum(float(r["ads_cost"] or 0) for r in channels),
            "live_cost": sum(float(r["live_cost"] or 0) for r in channels),
            "payout_estimated": sum(float(r["payout_estimated"] or 0) for r in channels),
            "profit": sum(float(r["profit"] or 0) for r in channels),
        }
        total["profit_margin_pct"] = round(total["profit"] / total["revenue"] * 100, 2) if total["revenue"] else 0
        return {"status": "success", "period": {"from": tu_ngay, "to": den_ngay}, "total": total, "channels": channels}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur: cur.close()
        if conn: conn.close()
