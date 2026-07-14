from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import RealDictCursor
from typing import Optional
from api.database import get_db_connection

router = APIRouter(prefix="/api/v1/reports", tags=["Nhanh.vn"])

NHANH_DAILY_SQL = """
WITH item_by_order AS (
    SELECT
        oi.order_id,
        SUM(COALESCE(oi.quantity, 0) * COALESCE(oi.unit_cogs, 0)) AS cogs,
        SUM(COALESCE(oi.quantity, 0)) AS qty
    FROM nhanh.order_items oi
    GROUP BY oi.order_id
), daily AS (
    SELECT
        o.order_date,
        COALESCE(o.label, 'DEFAULT') AS label,
        COUNT(o.id) AS total_orders,
        COUNT(o.id) FILTER (WHERE COALESCE(o.is_cancelled, false) OR o.status IN ('Đã hoàn','Da hoan')) AS canceled_orders,
        COUNT(o.id) FILTER (WHERE NOT (COALESCE(o.is_cancelled, false) OR o.status IN ('Đã hoàn','Da hoan'))) AS success_orders,
        SUM(COALESCE(o.order_value, 0)) AS net_revenue,
        SUM(COALESCE(i.cogs, 0)) AS net_cogs,
        SUM(COALESCE(i.qty, 0)) AS net_quantity,
        SUM(COALESCE(o.shipping_fee, 0)) AS shipping_fee,
        SUM(GREATEST(COALESCE(o.order_value, 0), 0) * 0.15) AS backoffice_fee,
        SUM(COALESCE(i.qty, 0) * 2000) AS packaging_cost
    FROM nhanh.orders o
    LEFT JOIN item_by_order i ON i.order_id = o.id
    {where_orders}
    GROUP BY o.order_date, COALESCE(o.label, 'DEFAULT')
), ads AS (
    SELECT ads_date, shop_label, SUM(COALESCE(cost_vnd_vat, 0)) AS ads_cost
    FROM nhanh.ads_costs
    {where_ads}
    GROUP BY ads_date, shop_label
)
SELECT
    COALESCE(d.order_date, a.ads_date) AS order_date,
    COALESCE(d.label, a.shop_label) AS label,
    COALESCE(d.total_orders, 0) AS total_orders,
    COALESCE(d.canceled_orders, 0) AS canceled_orders,
    COALESCE(d.success_orders, 0) AS success_orders,
    COALESCE(d.net_revenue, 0) AS net_revenue,
    COALESCE(d.net_cogs, 0) AS net_cogs,
    COALESCE(d.net_quantity, 0) AS net_quantity,
    COALESCE(d.shipping_fee, 0) AS shipping_fee,
    COALESCE(d.backoffice_fee, 0) AS backoffice_fee,
    COALESCE(d.packaging_cost, 0) AS packaging_cost,
    COALESCE(a.ads_cost, 0) AS ads_cost,
    (
        COALESCE(d.net_revenue, 0)
        - COALESCE(d.net_cogs, 0)
        - COALESCE(d.shipping_fee, 0)
        - COALESCE(d.backoffice_fee, 0)
        - COALESCE(d.packaging_cost, 0)
        - COALESCE(a.ads_cost, 0)
    ) AS profit
FROM daily d
FULL OUTER JOIN ads a ON a.ads_date = d.order_date AND a.shop_label = d.label
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

@router.get("/nhanh-period-total")
def get_nhanh_period_total(tu_ngay: str = Query(...), den_ngay: str = Query(...)):
    base = NHANH_DAILY_SQL.format(where_orders="WHERE o.order_date BETWEEN %s::date AND %s::date", where_ads="WHERE ads_date BETWEEN %s::date AND %s::date")
    sql = f"""
    WITH base AS ({base})
    SELECT
        %s || ' đến ' || %s AS "Giai đoạn",
        SUM(total_orders)::BIGINT AS "Tổng số đơn",
        SUM(canceled_orders)::BIGINT AS "Đơn hủy / Hoàn",
        SUM(success_orders)::BIGINT AS "Đơn thành công",
        SUM(net_revenue)::NUMERIC(18,2) AS "Doanh số",
        SUM(net_cogs)::NUMERIC(18,2) AS "Giá vốn",
        SUM(shipping_fee)::NUMERIC(18,2) AS "Phí vận chuyển/phí hoàn",
        SUM(backoffice_fee)::NUMERIC(18,2) AS "Backoff",
        SUM(packaging_cost)::NUMERIC(18,2) AS "Đóng gói",
        SUM(ads_cost)::NUMERIC(18,2) AS "Quảng cáo",
        SUM(profit)::NUMERIC(18,2) AS "Lợi nhuận",
        ROUND(SUM(profit) / NULLIF(SUM(net_revenue), 0) * 100, 2) AS "Biên LN (%)"
    FROM base
    """
    data = _fetch(sql, (tu_ngay, den_ngay, tu_ngay, den_ngay, tu_ngay, den_ngay))
    return {"status": "success", "data": data[0] if data else {}}

@router.get("/nhanh-period-label")
def get_nhanh_period_label(tu_ngay: str = Query(...), den_ngay: str = Query(...)):
    base = NHANH_DAILY_SQL.format(where_orders="WHERE o.order_date BETWEEN %s::date AND %s::date", where_ads="WHERE ads_date BETWEEN %s::date AND %s::date")
    sql = f"""
    WITH base AS ({base})
    SELECT
        label AS "Nhãn",
        SUM(total_orders)::BIGINT AS "Số đơn",
        SUM(success_orders)::BIGINT AS "Đơn thành công",
        SUM(net_revenue)::NUMERIC(18,2) AS "Doanh số",
        SUM(net_cogs)::NUMERIC(18,2) AS "Giá vốn",
        SUM(shipping_fee)::NUMERIC(18,2) AS "Phí vận chuyển/phí hoàn",
        SUM(backoffice_fee)::NUMERIC(18,2) AS "Backoff",
        SUM(packaging_cost)::NUMERIC(18,2) AS "Đóng gói",
        SUM(ads_cost)::NUMERIC(18,2) AS "Ads",
        SUM(profit)::NUMERIC(18,2) AS "Lợi nhuận"
    FROM base
    GROUP BY label
    ORDER BY "Lợi nhuận" DESC
    """
    return {"status": "success", "data": _fetch(sql, (tu_ngay, den_ngay, tu_ngay, den_ngay))}

@router.get("/nhanh-pnl")
def get_nhanh_pnl(ngay: Optional[str] = Query(None)):
    if ngay:
        base = NHANH_DAILY_SQL.format(where_orders="WHERE o.order_date = %s::date", where_ads="WHERE ads_date = %s::date")
        params = (ngay, ngay)
    else:
        base = NHANH_DAILY_SQL.format(where_orders="", where_ads="")
        params = ()
    sql = f"""
    WITH base AS ({base})
    SELECT
        order_date::text AS "Ngày",
        SUM(total_orders)::BIGINT AS "Số đơn",
        SUM(canceled_orders)::BIGINT AS "Đơn hủy",
        SUM(success_orders)::BIGINT AS "Đơn thành công",
        SUM(net_revenue)::NUMERIC(18,2) AS "Doanh số",
        SUM(net_cogs)::NUMERIC(18,2) AS "Giá vốn",
        SUM(shipping_fee)::NUMERIC(18,2) AS "Phí vận chuyển/phí hoàn",
        SUM(backoffice_fee)::NUMERIC(18,2) AS "Backoff",
        SUM(packaging_cost)::NUMERIC(18,2) AS "Đóng gói",
        SUM(ads_cost)::NUMERIC(18,2) AS "Ads",
        SUM(profit)::NUMERIC(18,2) AS "Lợi nhuận"
    FROM base
    GROUP BY order_date
    ORDER BY order_date DESC
    LIMIT 60
    """
    return {"status": "success", "data": _fetch(sql, params)}

@router.get("/nhanh-label-pnl")
def get_nhanh_label_pnl(ngay: Optional[str] = Query(None)):
    if ngay:
        base = NHANH_DAILY_SQL.format(where_orders="WHERE o.order_date = %s::date", where_ads="WHERE ads_date = %s::date")
        params = (ngay, ngay)
    else:
        base = NHANH_DAILY_SQL.format(where_orders="", where_ads="")
        params = ()
    sql = f"""
    WITH base AS ({base})
    SELECT
        order_date::text AS "Ngày",
        label AS "Nhãn",
        total_orders AS "Số đơn",
        success_orders AS "Đơn thành công",
        net_revenue AS "Doanh số",
        net_cogs AS "Giá vốn",
        shipping_fee AS "Phí vận chuyển/phí hoàn",
        backoffice_fee AS "Backoff",
        packaging_cost AS "Đóng gói",
        ads_cost AS "Ads",
        profit AS "Lợi nhuận"
    FROM base
    ORDER BY order_date DESC, profit DESC
    LIMIT 300
    """
    return {"status": "success", "data": _fetch(sql, params)}
