from fastapi import APIRouter, HTTPException, Query
from psycopg2.extras import RealDictCursor
from api.database import get_db_connection

router = APIRouter(prefix="/api/v1/mt-gt", tags=["MT-GT"])

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

def _date_where(alias="s", start_date=None, end_date=None):
    where = []
    params = []
    if start_date:
        where.append(f"{alias}.sale_date >= %s::date")
        params.append(start_date)
    if end_date:
        where.append(f"{alias}.sale_date <= %s::date")
        params.append(end_date)
    return ("WHERE " + " AND ".join(where) if where else "", params)

@router.get("/channel-summary")
def get_channel_summary(start_date: str = Query(None), end_date: str = Query(None)):
    where_sql, params = _date_where("s", start_date, end_date)
    sql = f"""
    SELECT
        s.channel_group AS channel,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) ELSE 0 END)::NUMERIC(18,3) AS total_qty,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END)::NUMERIC(18,2) AS total_revenue,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) * COALESCE(s.unit_cogs, 0) ELSE 0 END)::NUMERIC(18,2) AS total_cogs,
        (SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.02)::NUMERIC(18,2) AS shipping_fee,
        (SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.15)::NUMERIC(18,2) AS backoffice_fee,
        (
            SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END)
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) * COALESCE(s.unit_cogs, 0) ELSE 0 END)
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.02
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.15
        )::NUMERIC(18,2) AS gross_profit
    FROM mtgt.sales_lines s
    {where_sql}
    GROUP BY s.channel_group
    ORDER BY gross_profit DESC
    """
    return {"status": "success", "data": _fetch(sql, params)}

@router.get("/customer-summary")
def get_customer_summary(channel: str = Query(None, description="MT hoặc GT"), start_date: str = Query(None), end_date: str = Query(None)):
    where = []
    params = []
    if channel:
        where.append("s.channel_group = %s")
        params.append(channel.upper())
    if start_date:
        where.append("s.sale_date >= %s::date")
        params.append(start_date)
    if end_date:
        where.append("s.sale_date <= %s::date")
        params.append(end_date)
    where_sql = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
    SELECT
        s.channel_group AS channel,
        s.customer_code AS customer_id,
        s.customer_name,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) ELSE 0 END)::NUMERIC(18,3) AS qty,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END)::NUMERIC(18,2) AS revenue,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) * COALESCE(s.unit_cogs, 0) ELSE 0 END)::NUMERIC(18,2) AS cogs,
        (SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.02)::NUMERIC(18,2) AS shipping_fee,
        (SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.15)::NUMERIC(18,2) AS backoffice_fee,
        (
            SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END)
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) * COALESCE(s.unit_cogs, 0) ELSE 0 END)
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.02
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.15
        )::NUMERIC(18,2) AS net_profit
    FROM mtgt.sales_lines s
    {where_sql}
    GROUP BY s.channel_group, s.customer_code, s.customer_name
    ORDER BY net_profit DESC
    """
    return {"status": "success", "data": _fetch(sql, params)}

@router.get("/daily-trend")
def get_daily_trend(start_date: str = Query(None), end_date: str = Query(None)):
    where_sql, params = _date_where("s", start_date, end_date)
    sql = f"""
    SELECT
        s.sale_date::text AS sale_date,
        s.channel_group AS channel,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) ELSE 0 END)::NUMERIC(18,3) AS qty,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END)::NUMERIC(18,2) AS revenue,
        SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) * COALESCE(s.unit_cogs, 0) ELSE 0 END)::NUMERIC(18,2) AS cogs,
        (SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.02)::NUMERIC(18,2) AS shipping_fee,
        (SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.15)::NUMERIC(18,2) AS backoffice_fee,
        (
            SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END)
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.quantity, 0) * COALESCE(s.unit_cogs, 0) ELSE 0 END)
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.02
            - SUM(CASE WHEN COALESCE(s.line_type, '') <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.15
        )::NUMERIC(18,2) AS net_profit
    FROM mtgt.sales_lines s
    {where_sql}
    GROUP BY s.sale_date, s.channel_group
    ORDER BY s.sale_date ASC, s.channel_group ASC
    """
    return {"status": "success", "data": _fetch(sql, params)}
