from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import RealDictCursor
from typing import Optional
from api.database import get_db_connection

router = APIRouter(prefix="/api/v1/reports", tags=["Shopee"])

SHOPEE_DAILY_BASE = """
WITH item_by_order AS (
    SELECT
        oi.order_id,
        SUM(
            CASE
                WHEN COALESCE(o.is_cancelled, false) = true THEN 0
                ELSE COALESCE(oi.quantity, 0) * COALESCE(oi.unit_cogs, 0)
            END
        ) AS cogs,
        SUM(
            CASE
                WHEN COALESCE(o.is_cancelled, false) = true THEN 0
                ELSE COALESCE(oi.quantity, 0)
            END
        ) AS qty
    FROM shopee.order_items oi
    JOIN shopee.orders o ON o.id = oi.order_id
    GROUP BY oi.order_id
), order_base AS (
    SELECT
        o.id,
        o.order_date,
        o.shop_label,
        COALESCE(o.is_cancelled, false) AS is_cancelled,
        CASE WHEN COALESCE(o.is_cancelled, false) THEN 0 ELSE COALESCE(o.seller_revenue, 0) END AS net_revenue,
        COALESCE(i.cogs, 0) AS cogs,
        COALESCE(o.return_fee, 0) AS return_fee,
        COALESCE(o.fixed_fee, 0) + COALESCE(o.service_fee, 0) + COALESCE(o.payment_fee, 0) + COALESCE(o.affiliate_amount, 0) AS platform_fees,
        COALESCE(i.qty, 0) * 2000 AS packaging_cost,
        CASE
            WHEN COALESCE(o.payout_actual, 0) <> 0 THEN COALESCE(o.payout_actual, 0)
            ELSE COALESCE(o.est_payout, 0)
        END AS payout_estimated
    FROM shopee.orders o
    LEFT JOIN item_by_order i ON i.order_id = o.id
    {where_orders}
), daily AS (
    SELECT
        order_date,
        shop_label,
        COUNT(*) AS total_orders,
        COUNT(*) FILTER (WHERE is_cancelled) AS canceled_orders,
        COUNT(*) FILTER (WHERE NOT is_cancelled) AS success_orders,
        SUM(net_revenue) AS net_revenue,
        SUM(cogs) AS cogs,
        SUM(return_fee) AS return_fee,
        SUM(platform_fees) AS platform_fees,
        SUM(packaging_cost) AS packaging_cost,
        SUM(payout_estimated) AS payout_estimated,
        GREATEST(SUM(payout_estimated), 0) * 0.15 AS backoffice_fee
    FROM order_base
    GROUP BY order_date, shop_label
), ads AS (
    SELECT ads_date, shop_label, SUM(COALESCE(cost_vnd_vat, 0)) AS ads_cost
    FROM shopee.ads_costs
    {where_ads}
    GROUP BY ads_date, shop_label
)
SELECT
    COALESCE(d.order_date, a.ads_date) AS order_date,
    COALESCE(d.shop_label, a.shop_label) AS shop_label,
    COALESCE(d.total_orders, 0) AS total_orders,
    COALESCE(d.canceled_orders, 0) AS canceled_orders,
    COALESCE(d.success_orders, 0) AS success_orders,
    COALESCE(d.net_revenue, 0) AS net_revenue,
    COALESCE(d.cogs, 0) AS cogs,
    COALESCE(d.return_fee, 0) AS return_fee,
    COALESCE(d.platform_fees, 0) AS platform_fees,
    COALESCE(d.packaging_cost, 0) AS packaging_cost,
    COALESCE(d.payout_estimated, 0) AS payout_estimated,
    COALESCE(d.backoffice_fee, 0) AS backoffice_fee,
    COALESCE(a.ads_cost, 0) AS ads_cost,
    (
        COALESCE(d.net_revenue, 0)
        - COALESCE(d.cogs, 0)
        - COALESCE(d.return_fee, 0)
        - COALESCE(d.platform_fees, 0)
        - COALESCE(d.backoffice_fee, 0)
        - COALESCE(a.ads_cost, 0)
        - COALESCE(d.packaging_cost, 0)
    ) AS profit
FROM daily d
FULL OUTER JOIN ads a ON a.ads_date = d.order_date AND a.shop_label = d.shop_label
"""

def _fetch(sql, params):
    conn = None
    cur = None
    try:
        conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute(sql, params)
        return cur.fetchall()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if cur: cur.close()
        if conn: conn.close()

@router.get("/shopee-pnl")
def get_shopee_pnl(ngay: Optional[str] = Query(None)):
    if ngay:
        base = SHOPEE_DAILY_BASE.format(where_orders="WHERE o.order_date = %s::date", where_ads="WHERE ads_date = %s::date")
        params = (ngay, ngay)
    else:
        base = SHOPEE_DAILY_BASE.format(where_orders="", where_ads="")
        params = ()
    sql = f"""
    WITH base AS ({base})
    SELECT
        order_date::text AS "Ngày",
        shop_label AS "Shop",
        SUM(total_orders)::BIGINT AS total_orders,
        SUM(canceled_orders)::BIGINT AS canceled_orders,
        SUM(success_orders)::BIGINT AS success_orders,
        SUM(net_revenue)::NUMERIC(18,2) AS "Doanh số",
        SUM(cogs)::NUMERIC(18,2) AS "Giá vốn",
        SUM(return_fee)::NUMERIC(18,2) AS "Phí vận chuyển/phí hoàn",
        SUM(platform_fees)::NUMERIC(18,2) AS "Chi phí sàn",
        SUM(backoffice_fee)::NUMERIC(18,2) AS "BO (15% tiền nhận ước tính)",
        SUM(ads_cost)::NUMERIC(18,2) AS "Quảng cáo",
        SUM(packaging_cost)::NUMERIC(18,2) AS "Đóng gói",
        SUM(payout_estimated)::NUMERIC(18,2) AS "Tiền dự thu từ sàn",
        SUM(profit)::NUMERIC(18,2) AS "Lợi nhuận"
    FROM base
    GROUP BY order_date, shop_label
    ORDER BY order_date DESC, shop_label
    LIMIT 300
    """
    return {"status": "success", "data": _fetch(sql, params)}

@router.get("/shopee-period-total")
def get_shopee_period_total(tu_ngay: str = Query(...), den_ngay: str = Query(...)):
    base = SHOPEE_DAILY_BASE.format(where_orders="WHERE o.order_date BETWEEN %s::date AND %s::date", where_ads="WHERE ads_date BETWEEN %s::date AND %s::date")
    sql = f"""
    WITH base AS ({base})
    SELECT
        %s || ' -> ' || %s AS "Giai đoạn",
        SUM(total_orders)::BIGINT AS "Tổng số đơn",
        SUM(canceled_orders)::BIGINT AS "Đơn hủy",
        SUM(success_orders)::BIGINT AS "Đơn thành công",
        SUM(net_revenue)::NUMERIC(18,2) AS "Doanh số",
        SUM(cogs)::NUMERIC(18,2) AS "Giá vốn",
        SUM(return_fee)::NUMERIC(18,2) AS "Phí vận chuyển/phí hoàn",
        SUM(platform_fees)::NUMERIC(18,2) AS "Chi phí sàn",
        SUM(backoffice_fee)::NUMERIC(18,2) AS "BO (15% tiền nhận ước tính)",
        SUM(ads_cost)::NUMERIC(18,2) AS "Quảng cáo",
        SUM(packaging_cost)::NUMERIC(18,2) AS "Đóng gói",
        SUM(payout_estimated)::NUMERIC(18,2) AS "Tiền dự thu từ sàn",
        SUM(profit)::NUMERIC(18,2) AS "Lợi nhuận",
        ROUND(SUM(profit) / NULLIF(SUM(net_revenue), 0) * 100, 2) AS "Biên LN (%)"
    FROM base
    """
    data = _fetch(sql, (tu_ngay, den_ngay, tu_ngay, den_ngay, tu_ngay, den_ngay))
    return {"status": "success", "data": data[0] if data else {}}
