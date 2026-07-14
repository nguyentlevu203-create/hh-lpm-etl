from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import RealDictCursor
from typing import Optional
from api.database import get_db_connection

router = APIRouter(prefix="/api/v1/reports", tags=["TikTok"])

TIKTOK_DAILY_BASE = """
WITH order_daily AS (
    SELECT
        o.order_date,
        o.shop_label,
        COUNT(*) AS total_orders,
        COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled, false)) AS canceled_orders,
        COUNT(*) FILTER (WHERE NOT COALESCE(o.is_cancelled, false)) AS success_orders,
        SUM(COALESCE(o.fee_base_revenue, 0)) AS gross_revenue,
        SUM(CASE WHEN COALESCE(o.is_cancelled, false) THEN COALESCE(o.fee_base_revenue, 0) ELSE 0 END) AS canceled_revenue,
        SUM(CASE WHEN COALESCE(o.is_cancelled, false) THEN 0 ELSE COALESCE(o.fee_base_revenue, 0) END) AS net_revenue,
        SUM(COALESCE(o.cogs_amount, 0)) AS cogs,
        SUM(COALESCE(o.return_shipping_fee, 0)) AS return_fee,
        SUM(COALESCE(o.fixed_fee, 0)) AS fixed_fee,
        SUM(COALESCE(o.payment_fee, 0)) AS payment_fee,
        SUM(COALESCE(o.vxp_fee, 0)) AS vxp_fee,
        SUM(COALESCE(o.infrastructure_fee, 0)) AS infra_fee,
        SUM(COALESCE(o.fixed_fee, 0) + COALESCE(o.payment_fee, 0) + COALESCE(o.vxp_fee, 0) + COALESCE(o.infrastructure_fee, 0)) AS platform_fees,
        SUM(COALESCE(o.commission_amount, 0)) AS commission,
        SUM(COALESCE(o.booking_fee, 0)) AS booking_fee,
        SUM(COALESCE(o.packaging_cost, 0)) AS packaging_cost,
        SUM(COALESCE(o.backoffice_cost, 0)) AS backoffice_cost,
        SUM(
            CASE
                WHEN COALESCE(o.settlement_paid, 0) + COALESCE(o.settlement_unpaid, 0) <> 0
                    THEN COALESCE(o.settlement_paid, 0) + COALESCE(o.settlement_unpaid, 0)
                ELSE COALESCE(o.estimated_payout, 0)
            END
        ) AS payout_estimated
    FROM tiktok.orders_pnl o
    {where_orders}
    GROUP BY o.order_date, o.shop_label
), ads AS (
    SELECT ads_date, shop_label, SUM(COALESCE(cost_vnd_vat, 0)) AS ads_cost
    FROM tiktok.ads_costs
    {where_ads}
    GROUP BY ads_date, shop_label
), live AS (
    SELECT live_date, shop_label, SUM(COALESCE(in_house_cost, 0)) AS in_house_cost, SUM(COALESCE(teaser_cost, 0)) AS teaser_cost
    FROM tiktok.live_costs
    {where_live}
    GROUP BY live_date, shop_label
)
SELECT
    COALESCE(o.order_date, a.ads_date, l.live_date) AS order_date,
    COALESCE(o.shop_label, a.shop_label, l.shop_label) AS shop_label,
    COALESCE(o.total_orders, 0) AS total_orders,
    COALESCE(o.canceled_orders, 0) AS canceled_orders,
    COALESCE(o.success_orders, 0) AS success_orders,
    COALESCE(o.gross_revenue, 0) AS gross_revenue,
    COALESCE(o.canceled_revenue, 0) AS canceled_revenue,
    COALESCE(o.net_revenue, 0) AS net_revenue,
    COALESCE(o.cogs, 0) AS cogs,
    COALESCE(o.return_fee, 0) AS return_fee,
    COALESCE(o.fixed_fee, 0) AS fixed_fee,
    COALESCE(o.payment_fee, 0) AS payment_fee,
    COALESCE(o.vxp_fee, 0) AS vxp_fee,
    COALESCE(o.infra_fee, 0) AS infra_fee,
    COALESCE(o.platform_fees, 0) AS platform_fees,
    COALESCE(o.commission, 0) AS commission,
    COALESCE(o.booking_fee, 0) AS booking_fee,
    COALESCE(o.packaging_cost, 0) AS packaging_cost,
    COALESCE(o.backoffice_cost, 0) AS backoffice_cost,
    COALESCE(o.payout_estimated, 0) AS payout_estimated,
    COALESCE(a.ads_cost, 0) AS ads_cost,
    COALESCE(l.teaser_cost, 0) AS teaser_cost,
    COALESCE(l.in_house_cost, 0) AS in_house_cost,
    COALESCE(a.ads_cost, 0) + COALESCE(l.teaser_cost, 0) AS total_ads_cost,
    (
        COALESCE(o.net_revenue, 0)
        - COALESCE(o.cogs, 0)
        - COALESCE(o.return_fee, 0)
        - COALESCE(o.platform_fees, 0)
        - COALESCE(o.backoffice_cost, 0)
        - COALESCE(o.booking_fee, 0)
        - (COALESCE(a.ads_cost, 0) + COALESCE(l.teaser_cost, 0))
        - COALESCE(o.packaging_cost, 0)
        - COALESCE(l.in_house_cost, 0)
    ) AS profit
FROM order_daily o
FULL OUTER JOIN ads a ON a.ads_date = o.order_date AND a.shop_label = o.shop_label
FULL OUTER JOIN live l ON l.live_date = COALESCE(o.order_date, a.ads_date) AND l.shop_label = COALESCE(o.shop_label, a.shop_label)
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

@router.get("/tiktok-period-total")
def get_tiktok_period_total(tu_ngay: str = Query(...), den_ngay: str = Query(...)):
    base = TIKTOK_DAILY_BASE.format(
        where_orders="WHERE o.order_date BETWEEN %s::date AND %s::date",
        where_ads="WHERE ads_date BETWEEN %s::date AND %s::date",
        where_live="WHERE live_date BETWEEN %s::date AND %s::date",
    )
    sql = f"""
    WITH base AS ({base})
    SELECT
        %s || ' -> ' || %s AS "Giai đoạn",
        SUM(total_orders)::BIGINT AS "Tổng số đơn",
        SUM(canceled_orders)::BIGINT AS "Số đơn đã hủy",
        SUM(success_orders)::BIGINT AS "Số đơn thành công",
        SUM(gross_revenue)::NUMERIC(18,2) AS "Tổng doanh số trước hủy",
        SUM(canceled_revenue)::NUMERIC(18,2) AS "Doanh số đơn hủy",
        SUM(net_revenue)::NUMERIC(18,2) AS "Doanh số sau hủy",
        SUM(cogs)::NUMERIC(18,2) AS "Giá vốn",
        SUM(return_fee)::NUMERIC(18,2) AS "Phí vận chuyển/phí hoàn",
        SUM(platform_fees)::NUMERIC(18,2) AS "Phí sàn",
        SUM(fixed_fee)::NUMERIC(18,2) AS "Phí cố định",
        SUM(payment_fee)::NUMERIC(18,2) AS "Phí thanh toán",
        SUM(vxp_fee)::NUMERIC(18,2) AS "Phí VXP",
        SUM(infra_fee)::NUMERIC(18,2) AS "Phí hạ tầng",
        SUM(commission)::NUMERIC(18,2) AS "Phí hoa hồng",
        SUM(backoffice_cost)::NUMERIC(18,2) AS "Back office",
        SUM(booking_fee)::NUMERIC(18,2) AS "Booking",
        SUM(ads_cost)::NUMERIC(18,2) AS "Ads cost",
        SUM(teaser_cost)::NUMERIC(18,2) AS "Teaser cost",
        SUM(total_ads_cost)::NUMERIC(18,2) AS "Quảng cáo",
        SUM(packaging_cost)::NUMERIC(18,2) AS "Đóng gói",
        SUM(in_house_cost)::NUMERIC(18,2) AS "Live in-house",
        SUM(payout_estimated)::NUMERIC(18,2) AS "Tiền dự thu từ sàn",
        SUM(profit)::NUMERIC(18,2) AS "Lợi nhuận",
        ROUND(SUM(profit) / NULLIF(SUM(net_revenue), 0) * 100, 2) AS "Biên LN (%)"
    FROM base
    """
    data = _fetch(sql, (tu_ngay, den_ngay, tu_ngay, den_ngay, tu_ngay, den_ngay, tu_ngay, den_ngay))
    return {"status": "success", "data": data[0] if data else {}}

@router.get("/tiktok-pnl")
def get_tiktok_pnl(ngay: Optional[str] = Query(None)):
    if ngay:
        base = TIKTOK_DAILY_BASE.format(where_orders="WHERE o.order_date = %s::date", where_ads="WHERE ads_date = %s::date", where_live="WHERE live_date = %s::date")
        params = (ngay, ngay, ngay)
    else:
        base = TIKTOK_DAILY_BASE.format(where_orders="", where_ads="", where_live="")
        params = ()
    sql = f"""
    WITH base AS ({base})
    SELECT
        order_date::text AS "Ngày",
        shop_label AS "Shop",
        SUM(total_orders)::BIGINT AS "Tổng số đơn",
        SUM(canceled_orders)::BIGINT AS "Số đơn đã hủy",
        SUM(success_orders)::BIGINT AS "Số đơn thành công",
        SUM(net_revenue)::NUMERIC(18,2) AS "Doanh số sau hủy",
        SUM(cogs)::NUMERIC(18,2) AS "Giá vốn",
        SUM(return_fee)::NUMERIC(18,2) AS "Phí vận chuyển/phí hoàn",
        SUM(platform_fees)::NUMERIC(18,2) AS "Phí sàn",
        SUM(backoffice_cost)::NUMERIC(18,2) AS "Back office",
        SUM(booking_fee)::NUMERIC(18,2) AS "Booking",
        SUM(ads_cost)::NUMERIC(18,2) AS "Ads cost",
        SUM(teaser_cost)::NUMERIC(18,2) AS "Teaser cost",
        SUM(total_ads_cost)::NUMERIC(18,2) AS "Quảng cáo",
        SUM(packaging_cost)::NUMERIC(18,2) AS "Đóng gói",
        SUM(in_house_cost)::NUMERIC(18,2) AS "Live in-house",
        SUM(payout_estimated)::NUMERIC(18,2) AS "Tiền dự thu từ sàn",
        SUM(profit)::NUMERIC(18,2) AS "Lợi nhuận"
    FROM base
    GROUP BY order_date, shop_label
    ORDER BY order_date DESC, shop_label
    LIMIT 300
    """
    return {"status": "success", "data": _fetch(sql, params)}
