# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import psycopg2
import psycopg2.extras


PACKAGE_TYPE = "CEO_GROWTH_WEEK_MONTH"
PACKAGE_VERSION = "v3.3"
PACKAGE_VERSION_INT = 33


# ============================================================
# JSON / formatting helpers
# ============================================================

def json_default(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def clean_value(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [clean_value(v) for v in value]
    if isinstance(value, dict):
        return {k: clean_value(v) for k, v in value.items()}
    return value


def rows_to_dicts(rows: Sequence[dict]) -> List[dict]:
    return [clean_value(dict(r)) for r in rows]


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def add_months(d: dt.date, months: int) -> dt.date:
    year = d.year + (d.month - 1 + months) // 12
    month = (d.month - 1 + months) % 12 + 1
    if month == 12:
        next_month = dt.date(year + 1, 1, 1)
    else:
        next_month = dt.date(year, month + 1, 1)
    last_day = (next_month - dt.timedelta(days=1)).day
    return dt.date(year, month, min(d.day, last_day))


def month_start(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def week_start_monday(d: dt.date) -> dt.date:
    return d - dt.timedelta(days=d.weekday())


def money(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value):,.0f}"
    except Exception:
        return str(value)


def pct(value: Any) -> str:
    if value is None:
        return "N/A"
    try:
        return f"{float(value) * 100:.1f}%"
    except Exception:
        return str(value)


# ============================================================
# DB helpers
# ============================================================

def connect():
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn)

    return psycopg2.connect(
        host=os.getenv("PGHOST", "localhost"),
        port=os.getenv("PGPORT", "5433"),
        dbname=os.getenv("PGDATABASE", "DuLieu"),
        user=os.getenv("PGUSER", "postgres"),
        password=os.getenv("PGPASSWORD", ""),
    )


def relation_exists(cur, schema: str, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{name}",))
    row = cur.fetchone()
    return bool(row and row["ok"])


def get_relation_columns(cur, schema: str, name: str) -> set[str]:
    if not relation_exists(cur, schema, name):
        return set()

    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = %s
          AND table_name = %s
        """,
        (schema, name),
    )
    return {str(r["column_name"]) for r in cur.fetchall()}


def fetch_view(
    cur,
    full_view_name: str,
    as_of_date: str,
    order_by: Optional[str] = None,
    limit: Optional[int] = None,
    required: bool = False,
    errors: Optional[List[str]] = None,
    warnings: Optional[List[str]] = None,
    label: Optional[str] = None,
) -> List[dict]:
    schema, view_name = full_view_name.split(".", 1)

    if not relation_exists(cur, schema, view_name):
        msg = f"Missing view {full_view_name}. Chạy migration SQL v3.0/v3.1 trước."
        if required and errors is not None:
            errors.append(msg)
        elif warnings is not None:
            warnings.append(msg)
        return []

    sql = f"SELECT * FROM {schema}.{view_name} WHERE as_of_date = %s"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"

    cur.execute(sql, (as_of_date,))
    rows = rows_to_dicts(cur.fetchall())

    if required and not rows and errors is not None:
        errors.append(f"Missing {label or full_view_name} data for report date.")
    elif not rows and warnings is not None:
        warnings.append(f"Missing {label or full_view_name} data for report date.")

    return rows



def fetch_view_latest_on_or_before(
    cur,
    full_view_name: str,
    as_of_date: str,
    order_by: Optional[str] = None,
    limit: Optional[int] = None,
    required: bool = False,
    errors: Optional[List[str]] = None,
    warnings: Optional[List[str]] = None,
    notes: Optional[List[str]] = None,
    label: Optional[str] = None,
) -> List[dict]:
    """
    Fetch rows for exact as_of_date if available; otherwise use the latest
    as_of_date <= report date. This is intended only for slow-moving monthly
    derived views such as MT-GT Top Customers, where the latest available
    trading date may be earlier than the report date.

    If fallback is used, record a note instead of warning so data_quality
    remains PASS when the data is available and intentionally carried forward.
    """
    schema, view_name = full_view_name.split(".", 1)

    if not relation_exists(cur, schema, view_name):
        msg = f"Missing view {full_view_name}. Chạy migration SQL v3.1 trước."
        if required and errors is not None:
            errors.append(msg)
        elif warnings is not None:
            warnings.append(msg)
        return []

    cur.execute(
        f"""
        SELECT MAX(as_of_date)::date AS latest_as_of_date
        FROM {schema}.{view_name}
        WHERE as_of_date <= %s
        """,
        (as_of_date,),
    )
    row = cur.fetchone()
    latest_as_of_date = row["latest_as_of_date"] if row else None

    if not latest_as_of_date:
        msg = f"Missing {label or full_view_name} data on or before report date {as_of_date}."
        if required and errors is not None:
            errors.append(msg)
        elif warnings is not None:
            warnings.append(msg)
        return []

    sql = f"SELECT * FROM {schema}.{view_name} WHERE as_of_date = %s"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit:
        sql += f" LIMIT {int(limit)}"

    cur.execute(sql, (latest_as_of_date,))
    rows = rows_to_dicts(cur.fetchall())

    if str(latest_as_of_date) != as_of_date and notes is not None:
        notes.append(
            f"{label or full_view_name} dùng dữ liệu gần nhất ngày {latest_as_of_date}, "
            f"không có đúng ngày báo cáo {as_of_date}."
        )

    if required and not rows and errors is not None:
        errors.append(f"Missing {label or full_view_name} data for latest available date.")
    elif not rows and warnings is not None:
        warnings.append(f"Missing {label or full_view_name} data for latest available date.")

    return rows


def find_total(rows: List[dict], channel_field: str = "channel") -> Optional[dict]:
    for r in rows:
        if str(r.get(channel_field, "")).upper() == "TOTAL":
            return r
    return rows[0] if len(rows) == 1 else None


def fetch_affiliate_cost_by_channel(
    cur,
    week_start: dt.date,
    report_date: dt.date,
    month_start_date: dt.date,
    warnings: List[str],
) -> List[dict]:
    """
    Supplemental affiliate/commission metrics for week/month reports.

    Shopee affiliate is already included in platform_fees by the CEO daily base.
    TikTok commission_amount is stored separately in tiktok.orders_pnl and is not
    included in the CEO platform_fees expression in the current source code.
    """
    rows: List[dict] = []

    shopee_columns = get_relation_columns(cur, "shopee", "orders")
    if {"order_date", "affiliate_amount"}.issubset(shopee_columns):
        shop_expr = "COALESCE(shop_label, 'SHOPEE')" if "shop_label" in shopee_columns else "'SHOPEE'"
        cancelled_expr = "COALESCE(is_cancelled, false)" if "is_cancelled" in shopee_columns else "false"
        cur.execute(
            f"""
            SELECT
                %s::date AS as_of_date,
                'SHOPEE'::text AS channel,
                {shop_expr}::text AS shop_label,
                SUM(
                    CASE
                        WHEN order_date BETWEEN %s AND %s AND NOT ({cancelled_expr})
                        THEN COALESCE(affiliate_amount, 0)
                        ELSE 0
                    END
                )::numeric AS affiliate_cost_wtd,
                COUNT(*) FILTER (
                    WHERE order_date BETWEEN %s AND %s
                      AND NOT ({cancelled_expr})
                      AND COALESCE(affiliate_amount, 0) <> 0
                )::numeric AS affiliate_orders_wtd,
                SUM(
                    CASE
                        WHEN order_date BETWEEN %s AND %s AND NOT ({cancelled_expr})
                        THEN COALESCE(affiliate_amount, 0)
                        ELSE 0
                    END
                )::numeric AS affiliate_cost_mtd,
                COUNT(*) FILTER (
                    WHERE order_date BETWEEN %s AND %s
                      AND NOT ({cancelled_expr})
                      AND COALESCE(affiliate_amount, 0) <> 0
                )::numeric AS affiliate_orders_mtd,
                'shopee.orders.affiliate_amount'::text AS source_column,
                true AS included_in_current_platform_fees,
                'Shopee affiliate đã nằm trong platform_fees hiện tại; chỉ hiển thị riêng, không cộng trùng.'::text AS counting_note
            FROM shopee.orders
            WHERE order_date BETWEEN %s AND %s
            GROUP BY {shop_expr}
            ORDER BY affiliate_cost_mtd DESC NULLS LAST
            """,
            (
                report_date,
                week_start,
                report_date,
                week_start,
                report_date,
                month_start_date,
                report_date,
                month_start_date,
                report_date,
                month_start_date,
                report_date,
            ),
        )
        rows.extend(rows_to_dicts(cur.fetchall()))
    else:
        warnings.append("Missing shopee.orders.affiliate_amount; cannot include Shopee affiliate in week/month package.")

    tiktok_columns = get_relation_columns(cur, "tiktok", "orders_pnl")
    if {"order_date", "commission_amount"}.issubset(tiktok_columns):
        shop_expr = "COALESCE(shop_label, 'TIKTOK')" if "shop_label" in tiktok_columns else "'TIKTOK'"
        cancelled_expr = "COALESCE(is_cancelled, false)" if "is_cancelled" in tiktok_columns else "false"
        cur.execute(
            f"""
            SELECT
                %s::date AS as_of_date,
                'TIKTOK'::text AS channel,
                {shop_expr}::text AS shop_label,
                SUM(
                    CASE
                        WHEN order_date BETWEEN %s AND %s AND NOT ({cancelled_expr})
                        THEN COALESCE(commission_amount, 0)
                        ELSE 0
                    END
                )::numeric AS affiliate_cost_wtd,
                COUNT(*) FILTER (
                    WHERE order_date BETWEEN %s AND %s
                      AND NOT ({cancelled_expr})
                      AND COALESCE(commission_amount, 0) <> 0
                )::numeric AS affiliate_orders_wtd,
                SUM(
                    CASE
                        WHEN order_date BETWEEN %s AND %s AND NOT ({cancelled_expr})
                        THEN COALESCE(commission_amount, 0)
                        ELSE 0
                    END
                )::numeric AS affiliate_cost_mtd,
                COUNT(*) FILTER (
                    WHERE order_date BETWEEN %s AND %s
                      AND NOT ({cancelled_expr})
                      AND COALESCE(commission_amount, 0) <> 0
                )::numeric AS affiliate_orders_mtd,
                'tiktok.orders_pnl.commission_amount'::text AS source_column,
                false AS included_in_current_platform_fees,
                'TikTok commission_amount đang là khoản riêng; báo cáo nên hiển thị affiliate/commission riêng nếu chưa nâng view lợi nhuận.'::text AS counting_note
            FROM tiktok.orders_pnl
            WHERE order_date BETWEEN %s AND %s
            GROUP BY {shop_expr}
            ORDER BY affiliate_cost_mtd DESC NULLS LAST
            """,
            (
                report_date,
                week_start,
                report_date,
                week_start,
                report_date,
                month_start_date,
                report_date,
                month_start_date,
                report_date,
                month_start_date,
                report_date,
            ),
        )
        rows.extend(rows_to_dicts(cur.fetchall()))
    else:
        warnings.append("Missing tiktok.orders_pnl.commission_amount; cannot include TikTok affiliate in week/month package.")

    total = {
        "as_of_date": report_date.isoformat(),
        "channel": "TOTAL",
        "shop_label": "TOTAL",
        "affiliate_cost_wtd": sum(float(r.get("affiliate_cost_wtd") or 0) for r in rows),
        "affiliate_orders_wtd": sum(float(r.get("affiliate_orders_wtd") or 0) for r in rows),
        "affiliate_cost_mtd": sum(float(r.get("affiliate_cost_mtd") or 0) for r in rows),
        "affiliate_orders_mtd": sum(float(r.get("affiliate_orders_mtd") or 0) for r in rows),
        "source_column": "shopee.orders.affiliate_amount + tiktok.orders_pnl.commission_amount",
        "included_in_current_platform_fees": None,
        "counting_note": "TOTAL là tổng để hiển thị; xem từng kênh để biết khoản nào đã nằm trong platform_fees.",
    }
    return clean_value([total] + rows)


# ============================================================
# Daily trend builders for 2-line charts
# ============================================================

def fetch_daily_trend_fallback_from_daily_base(
    cur,
    start_date: dt.date,
    end_date: dt.date,
    warnings: List[str],
    label: str,
) -> List[dict]:
    """
    Fallback từ control_tower.v_growth_daily_base nếu view trend theo as_of_date
    không có dữ liệu cho kỳ cần so sánh.

    Output chuẩn hóa:
      as_of_date, period_start, period_end, trend_date, channel,
      gross_sales, net_sales, orders, profit, profit_margin_pct
    """
    if not relation_exists(cur, "control_tower", "v_growth_daily_base"):
        warnings.append(f"Missing control_tower.v_growth_daily_base; cannot fallback {label}.")
        return []

    sql = """
        WITH base AS (
            SELECT
                report_date::date AS trend_date,
                channel::text AS channel,
                SUM(COALESCE(gross_sales, 0))::numeric AS gross_sales,
                SUM(COALESCE(net_sales, 0))::numeric AS net_sales,
                SUM(COALESCE(orders, 0))::numeric AS orders,
                SUM(COALESCE(profit, 0))::numeric AS profit
            FROM control_tower.v_growth_daily_base
            WHERE report_date BETWEEN %s AND %s
            GROUP BY report_date::date, channel::text
        ),
        total AS (
            SELECT
                trend_date,
                'TOTAL'::text AS channel,
                SUM(gross_sales)::numeric AS gross_sales,
                SUM(net_sales)::numeric AS net_sales,
                SUM(orders)::numeric AS orders,
                SUM(profit)::numeric AS profit
            FROM base
            GROUP BY trend_date
        ),
        unioned AS (
            SELECT * FROM total
            UNION ALL
            SELECT * FROM base
        )
        SELECT
            %s::date AS as_of_date,
            %s::date AS period_start,
            %s::date AS period_end,
            trend_date,
            channel,
            gross_sales,
            net_sales,
            orders,
            profit,
            CASE WHEN net_sales <> 0 THEN profit / net_sales ELSE NULL END AS profit_margin_pct
        FROM unioned
        ORDER BY trend_date, CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel;
    """
    cur.execute(sql, (start_date, end_date, end_date, start_date, end_date))
    return rows_to_dicts(cur.fetchall())


def fetch_monthly_trend_by_as_of(cur, as_of_date: dt.date, warnings: List[str], label: str) -> List[dict]:
    rows = fetch_view(
        cur,
        "control_tower.v_growth_monthly_daily_trend",
        as_of_date.isoformat(),
        order_by="trend_date, CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel",
        warnings=warnings,
        label=label,
    )
    return rows


def fetch_weekly_trend_by_as_of(cur, as_of_date: dt.date, warnings: List[str], label: str) -> List[dict]:
    rows = fetch_view(
        cur,
        "control_tower.v_growth_weekly_daily_trend",
        as_of_date.isoformat(),
        order_by="trend_date, CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel",
        warnings=warnings,
        label=label,
    )
    return rows


def get_total_map(rows: List[dict]) -> Dict[str, dict]:
    return {
        str(r.get("trend_date")): r
        for r in rows
        if str(r.get("channel", "")).upper() == "TOTAL"
    }


def build_total_current_vs_previous(
    current_rows: List[dict],
    previous_rows: List[dict],
    current_start: dt.date,
    current_end: dt.date,
    previous_start: dt.date,
    previous_end: dt.date,
) -> List[dict]:
    current_total = get_total_map(current_rows)
    previous_total = get_total_map(previous_rows)

    days = (current_end - current_start).days + 1
    out: List[dict] = []

    for i in range(days):
        current_date = current_start + dt.timedelta(days=i)
        previous_date = previous_start + dt.timedelta(days=i)

        if previous_date > previous_end:
            p = {}
        else:
            p = previous_total.get(previous_date.isoformat()) or {}

        c = current_total.get(current_date.isoformat()) or {}

        current_net = c.get("net_sales")
        previous_net = p.get("net_sales")

        out.append(
            {
                "day_no": i + 1,
                "current_date": current_date.isoformat(),
                "previous_date": previous_date.isoformat() if previous_date <= previous_end else None,
                "current_net_sales": current_net,
                "previous_net_sales": previous_net,
                "current_gross_sales": c.get("gross_sales"),
                "previous_gross_sales": p.get("gross_sales"),
                "current_profit": c.get("profit"),
                "previous_profit": p.get("profit"),
                "current_orders": c.get("orders"),
                "previous_orders": p.get("orders"),
                "net_sales_delta": None if current_net is None or previous_net is None else current_net - previous_net,
            }
        )

    return clean_value(out)


def build_channel_current_vs_previous(
    current_rows: List[dict],
    previous_rows: List[dict],
    current_start: dt.date,
    current_end: dt.date,
    previous_start: dt.date,
    previous_end: dt.date,
) -> List[dict]:
    current_map: Dict[Tuple[str, str], dict] = {}
    previous_map: Dict[Tuple[str, str], dict] = {}
    channels = set()

    for r in current_rows:
        channel = str(r.get("channel", "")).upper()
        if not channel or channel == "TOTAL":
            continue
        trend_date = str(r.get("trend_date"))
        current_map[(trend_date, channel)] = r
        channels.add(channel)

    for r in previous_rows:
        channel = str(r.get("channel", "")).upper()
        if not channel or channel == "TOTAL":
            continue
        trend_date = str(r.get("trend_date"))
        previous_map[(trend_date, channel)] = r
        channels.add(channel)

    days = (current_end - current_start).days + 1
    out: List[dict] = []

    for channel in sorted(channels):
        for i in range(days):
            current_date = current_start + dt.timedelta(days=i)
            previous_date = previous_start + dt.timedelta(days=i)

            c = current_map.get((current_date.isoformat(), channel)) or {}
            if previous_date > previous_end:
                p = {}
            else:
                p = previous_map.get((previous_date.isoformat(), channel)) or {}

            current_net = c.get("net_sales")
            previous_net = p.get("net_sales")

            out.append(
                {
                    "channel": channel,
                    "day_no": i + 1,
                    "current_date": current_date.isoformat(),
                    "previous_date": previous_date.isoformat() if previous_date <= previous_end else None,
                    "current_net_sales": current_net,
                    "previous_net_sales": previous_net,
                    "current_gross_sales": c.get("gross_sales"),
                    "previous_gross_sales": p.get("gross_sales"),
                    "current_profit": c.get("profit"),
                    "previous_profit": p.get("profit"),
                    "current_orders": c.get("orders"),
                    "previous_orders": p.get("orders"),
                    "net_sales_delta": None if current_net is None or previous_net is None else current_net - previous_net,
                }
            )

    return clean_value(out)


def summarize_daily_coverage(total_compare_rows: List[dict], channel_compare_rows: List[dict]) -> dict:
    total_prev_days = sum(1 for r in total_compare_rows if r.get("previous_net_sales") is not None)
    total_current_days = sum(1 for r in total_compare_rows if r.get("current_net_sales") is not None)

    channel_names = sorted({r.get("channel") for r in channel_compare_rows if r.get("channel")})
    by_channel = {}

    for channel in channel_names:
        rows = [r for r in channel_compare_rows if r.get("channel") == channel]
        by_channel[channel] = {
            "days": len(rows),
            "current_days_with_data": sum(1 for r in rows if r.get("current_net_sales") is not None),
            "previous_days_with_data": sum(1 for r in rows if r.get("previous_net_sales") is not None),
        }

    return {
        "total_days": len(total_compare_rows),
        "current_total_days_with_data": total_current_days,
        "previous_total_days_with_data": total_prev_days,
        "has_previous_daily_total_series": total_prev_days > 0,
        "channels": by_channel,
    }


# ============================================================
# Package builder
# ============================================================

def build_package(report_date_str: str) -> Dict[str, Any]:
    report_date = parse_date(report_date_str)

    current_month_start = month_start(report_date)
    previous_month_end = add_months(report_date, -1)
    previous_month_start = month_start(previous_month_end)

    current_week_start = week_start_monday(report_date)
    previous_week_start = current_week_start - dt.timedelta(days=7)
    previous_week_end = report_date - dt.timedelta(days=7)

    errors: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []

    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # ---------------- Weekly core ----------------
            weekly_summary_rows = fetch_view(
                cur,
                "control_tower.v_growth_weekly_summary",
                report_date_str,
                order_by="CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel",
                required=True,
                errors=errors,
                label="weekly total summary",
            )
            weekly_summary = find_total(weekly_summary_rows)

            weekly_comparison_rows = fetch_view(
                cur,
                "control_tower.v_growth_weekly_comparison",
                report_date_str,
                order_by="CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel",
                warnings=warnings,
                label="weekly comparison",
            )
            weekly_comparison = find_total(weekly_comparison_rows)

            weekly_daily_trend = fetch_weekly_trend_by_as_of(
                cur,
                report_date,
                warnings,
                "weekly daily trend current period",
            )
            if not weekly_daily_trend:
                warnings.append("Current weekly daily trend view returned no rows; trying fallback from control_tower.v_growth_daily_base.")
                weekly_daily_trend = fetch_daily_trend_fallback_from_daily_base(
                    cur, current_week_start, report_date, warnings, "current weekly daily trend"
                )

            previous_weekly_daily_trend = fetch_weekly_trend_by_as_of(
                cur,
                previous_week_end,
                warnings,
                "weekly daily trend previous same period",
            )
            if not previous_weekly_daily_trend:
                warnings.append("Previous weekly daily trend view returned no rows; trying fallback from control_tower.v_growth_daily_base.")
                previous_weekly_daily_trend = fetch_daily_trend_fallback_from_daily_base(
                    cur, previous_week_start, previous_week_end, warnings, "previous weekly daily trend"
                )

            weekly_scorecard_by_channel = fetch_view(
                cur,
                "control_tower.v_growth_weekly_by_channel",
                report_date_str,
                order_by="net_sales_wtd DESC NULLS LAST",
                warnings=warnings,
                label="weekly scorecard by channel",
            )

            cost_breakdown_weekly = fetch_view(
                cur,
                "control_tower.v_growth_cost_breakdown_weekly",
                report_date_str,
                order_by="CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel, ABS(amount) DESC",
                warnings=warnings,
                label="weekly cost breakdown",
            )

            # ---------------- Monthly core ----------------
            monthly_summary_rows = fetch_view(
                cur,
                "control_tower.v_growth_monthly_summary",
                report_date_str,
                order_by="CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel",
                required=True,
                errors=errors,
                label="monthly total summary",
            )
            monthly_summary = find_total(monthly_summary_rows)

            monthly_comparison_rows = fetch_view(
                cur,
                "control_tower.v_growth_monthly_comparison",
                report_date_str,
                order_by="CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel",
                warnings=warnings,
                label="monthly comparison",
            )
            monthly_comparison = find_total(monthly_comparison_rows)

            monthly_daily_trend = fetch_monthly_trend_by_as_of(
                cur,
                report_date,
                warnings,
                "monthly daily trend current period",
            )
            if not monthly_daily_trend:
                warnings.append("Current monthly daily trend view returned no rows; trying fallback from control_tower.v_growth_daily_base.")
                monthly_daily_trend = fetch_daily_trend_fallback_from_daily_base(
                    cur, current_month_start, report_date, warnings, "current monthly daily trend"
                )

            previous_monthly_daily_trend = fetch_monthly_trend_by_as_of(
                cur,
                previous_month_end,
                warnings,
                "monthly daily trend previous same period",
            )
            if not previous_monthly_daily_trend:
                warnings.append("Previous monthly daily trend view returned no rows; trying fallback from control_tower.v_growth_daily_base.")
                previous_monthly_daily_trend = fetch_daily_trend_fallback_from_daily_base(
                    cur, previous_month_start, previous_month_end, warnings, "previous monthly daily trend"
                )

            monthly_scorecard_by_channel = fetch_view(
                cur,
                "control_tower.v_growth_monthly_by_channel",
                report_date_str,
                order_by="net_sales_mtd DESC NULLS LAST",
                warnings=warnings,
                label="monthly scorecard by channel",
            )

            monthly_target_forecast_rows = fetch_view(
                cur,
                "control_tower.v_growth_monthly_target_forecast",
                report_date_str,
                order_by="CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel",
                warnings=warnings,
                label="monthly target forecast",
            )
            monthly_target_forecast = find_total(monthly_target_forecast_rows)

            cost_breakdown_monthly = fetch_view(
                cur,
                "control_tower.v_growth_cost_breakdown_monthly",
                report_date_str,
                order_by="CASE WHEN channel='TOTAL' THEN 0 ELSE 1 END, channel, ABS(amount) DESC",
                warnings=warnings,
                label="monthly cost breakdown",
            )

            # ---------------- SKU / chart/customer extras ----------------
            monthly_top_sku_sales_level = fetch_view(
                cur,
                "mart.v_growth_top_sku_monthly_sales_level",
                report_date_str,
                order_by="rank_by_qty, rank_by_net_sales",
                limit=50,
                warnings=warnings,
                label="monthly Top SKU sales level",
            )

            monthly_top_sku_by_net_sales = fetch_view(
                cur,
                "mart.v_growth_top_sku_monthly_sales_level",
                report_date_str,
                order_by="rank_by_net_sales, rank_by_qty",
                limit=50,
            )

            monthly_top_sku_by_channel = fetch_view(
                cur,
                "mart.v_growth_top_sku_monthly_by_channel",
                report_date_str,
                order_by="channel, rank_by_qty, rank_by_net_sales",
                limit=200,
                warnings=warnings,
                label="monthly Top SKU by channel",
            )

            monthly_channel_sales_share_chart = fetch_view(
                cur,
                "control_tower.v_growth_monthly_channel_sales_share_chart",
                report_date_str,
                order_by="net_sales_mtd DESC NULLS LAST",
                warnings=warnings,
                label="monthly channel sales share chart",
            )

            monthly_channel_profit_share_chart = fetch_view(
                cur,
                "control_tower.v_growth_monthly_channel_profit_share_chart",
                report_date_str,
                order_by="profit_mtd DESC NULLS LAST",
                warnings=warnings,
                label="monthly channel profit share chart",
            )

            monthly_total_sales_trend_chart = fetch_view(
                cur,
                "control_tower.v_growth_monthly_total_sales_trend_chart",
                report_date_str,
                order_by="trend_date",
                warnings=warnings,
                label="monthly total sales trend chart",
            )

            monthly_channel_sales_trend_chart = fetch_view(
                cur,
                "control_tower.v_growth_monthly_channel_sales_trend_chart",
                report_date_str,
                order_by="trend_date, channel",
                warnings=warnings,
                label="monthly channel sales trend chart",
            )

            monthly_top_mtgt_customers = fetch_view_latest_on_or_before(
                cur,
                "mart.v_growth_mtgt_top5_customers_monthly",
                report_date_str,
                order_by="rank_overall_by_net_sales",
                warnings=warnings,
                notes=notes,
                label="Top 5 MT-GT customers",
            )

            monthly_top_mtgt_customers_by_channel = fetch_view_latest_on_or_before(
                cur,
                "mart.v_growth_mtgt_top5_customers_monthly_by_channel",
                report_date_str,
                order_by="channel_group, rank_channel_by_net_sales",
                warnings=warnings,
                notes=notes,
                label="Top 5 MT-GT customers by channel",
            )

            affiliate_cost_by_channel = fetch_affiliate_cost_by_channel(
                cur,
                current_week_start,
                report_date,
                current_month_start,
                warnings,
            )

    if not weekly_summary:
        errors.append("Missing weekly total summary for report date.")
    if not monthly_summary:
        errors.append("Missing monthly total summary for report date.")

    # Build 2-line chart data for week and month
    monthly_total_sales_trend_current_vs_previous = build_total_current_vs_previous(
        monthly_daily_trend,
        previous_monthly_daily_trend,
        current_month_start,
        report_date,
        previous_month_start,
        previous_month_end,
    )

    monthly_channel_sales_trend_current_vs_previous = build_channel_current_vs_previous(
        monthly_daily_trend,
        previous_monthly_daily_trend,
        current_month_start,
        report_date,
        previous_month_start,
        previous_month_end,
    )

    weekly_total_sales_trend_current_vs_previous = build_total_current_vs_previous(
        weekly_daily_trend,
        previous_weekly_daily_trend,
        current_week_start,
        report_date,
        previous_week_start,
        previous_week_end,
    )

    weekly_channel_sales_trend_current_vs_previous = build_channel_current_vs_previous(
        weekly_daily_trend,
        previous_weekly_daily_trend,
        current_week_start,
        report_date,
        previous_week_start,
        previous_week_end,
    )

    monthly_daily_coverage = summarize_daily_coverage(
        monthly_total_sales_trend_current_vs_previous,
        monthly_channel_sales_trend_current_vs_previous,
    )

    weekly_daily_coverage = summarize_daily_coverage(
        weekly_total_sales_trend_current_vs_previous,
        weekly_channel_sales_trend_current_vs_previous,
    )

    if not monthly_daily_coverage["has_previous_daily_total_series"]:
        warnings.append("No previous daily total series available for two-line monthly chart.")
    if not weekly_daily_coverage["has_previous_daily_total_series"]:
        warnings.append("No previous daily total series available for two-line weekly chart.")

    weekly_comparison_available = bool(
        weekly_comparison and weekly_comparison.get("comparison_available")
    )
    monthly_comparison_available = bool(
        monthly_comparison and monthly_comparison.get("comparison_available")
    )

    if weekly_comparison and not weekly_comparison_available:
        notes.append(str(weekly_comparison.get("comparison_note") or "Weekly comparison unavailable."))

    if monthly_comparison and not monthly_comparison_available:
        notes.append(str(monthly_comparison.get("comparison_note") or "Monthly comparison unavailable."))

    profit_pie_chart_usable = bool(monthly_channel_profit_share_chart) and all(
        bool(r.get("can_use_profit_pie_chart"))
        for r in monthly_channel_profit_share_chart
    )

    # Chart-ready derived payloads
    weekly_net_sales_trend = [
        {
            "date": r.get("trend_date"),
            "net_sales": r.get("net_sales"),
            "gross_sales": r.get("gross_sales"),
            "profit": r.get("profit"),
            "orders": r.get("orders"),
        }
        for r in weekly_daily_trend
        if str(r.get("channel", "")).upper() == "TOTAL"
    ]

    monthly_net_sales_trend = [
        {
            "date": r.get("trend_date"),
            "net_sales": r.get("net_sales"),
            "gross_sales": r.get("gross_sales"),
            "profit": r.get("profit"),
            "orders": r.get("orders"),
        }
        for r in monthly_total_sales_trend_chart
    ]

    monthly_channel_sales_lines = [
        {
            "date": r.get("trend_date"),
            "channel": r.get("channel"),
            "net_sales": r.get("net_sales"),
            "gross_sales": r.get("gross_sales"),
            "profit": r.get("profit"),
            "orders": r.get("orders"),
        }
        for r in monthly_channel_sales_trend_chart
    ]

    weekly_total_two_line_net_sales = [
        {
            "day_no": r.get("day_no"),
            "current_date": r.get("current_date"),
            "previous_date": r.get("previous_date"),
            "current_net_sales": r.get("current_net_sales"),
            "previous_net_sales": r.get("previous_net_sales"),
            "net_sales_delta": r.get("net_sales_delta"),
        }
        for r in weekly_total_sales_trend_current_vs_previous
    ]

    weekly_channel_two_line_net_sales = [
        {
            "channel": r.get("channel"),
            "day_no": r.get("day_no"),
            "current_date": r.get("current_date"),
            "previous_date": r.get("previous_date"),
            "current_net_sales": r.get("current_net_sales"),
            "previous_net_sales": r.get("previous_net_sales"),
            "net_sales_delta": r.get("net_sales_delta"),
        }
        for r in weekly_channel_sales_trend_current_vs_previous
    ]

    monthly_total_two_line_net_sales = [
        {
            "day_no": r.get("day_no"),
            "current_date": r.get("current_date"),
            "previous_date": r.get("previous_date"),
            "current_net_sales": r.get("current_net_sales"),
            "previous_net_sales": r.get("previous_net_sales"),
            "net_sales_delta": r.get("net_sales_delta"),
        }
        for r in monthly_total_sales_trend_current_vs_previous
    ]

    monthly_channel_two_line_net_sales = [
        {
            "channel": r.get("channel"),
            "day_no": r.get("day_no"),
            "current_date": r.get("current_date"),
            "previous_date": r.get("previous_date"),
            "current_net_sales": r.get("current_net_sales"),
            "previous_net_sales": r.get("previous_net_sales"),
            "net_sales_delta": r.get("net_sales_delta"),
        }
        for r in monthly_channel_sales_trend_current_vs_previous
    ]

    monthly_sales_share_pie = [
        {
            "channel": r.get("channel"),
            "net_sales_mtd": r.get("net_sales_mtd"),
            "share_percent": r.get("channel_share_percent"),
        }
        for r in monthly_channel_sales_share_chart
    ]

    monthly_profit_share_or_bar = [
        {
            "channel": r.get("channel"),
            "profit_mtd": r.get("profit_mtd"),
            "profit_share_percent": r.get("profit_share_percent"),
            "profit_margin_pct": r.get("profit_margin_pct_mtd"),
            "can_use_profit_pie_chart": r.get("can_use_profit_pie_chart"),
            "chart_note": r.get("chart_note"),
        }
        for r in monthly_channel_profit_share_chart
    ]

    top_mtgt_customers = [
        {
            "rank": r.get("rank_overall_by_net_sales"),
            "channel_group": r.get("channel_group"),
            "customer_code": r.get("customer_code"),
            "customer_name": r.get("customer_name"),
            "documents_mtd": r.get("documents_mtd"),
            "qty_mtd": r.get("qty_mtd"),
            "gross_sales_mtd": r.get("gross_sales_mtd"),
            "net_sales_mtd": r.get("net_sales_mtd"),
            "net_sales_prev_mtd": r.get("net_sales_prev_mtd"),
            "net_sales_delta": r.get("net_sales_delta"),
            "net_sales_delta_pct": r.get("net_sales_delta_pct"),
            "comparison_available": r.get("comparison_available"),
            "comparison_note": r.get("comparison_note"),
        }
        for r in monthly_top_mtgt_customers
    ]

    top_mtgt_customers_by_channel = [
        {
            "rank": r.get("rank_channel_by_net_sales"),
            "rank_overall": r.get("rank_overall_by_net_sales"),
            "channel_group": str(r.get("channel_group") or "").upper(),
            "customer_code": r.get("customer_code"),
            "customer_name": r.get("customer_name"),
            "documents_mtd": r.get("documents_mtd"),
            "qty_mtd": r.get("qty_mtd"),
            "gross_sales_mtd": r.get("gross_sales_mtd"),
            "net_sales_mtd": r.get("net_sales_mtd"),
            "net_sales_prev_mtd": r.get("net_sales_prev_mtd"),
            "net_sales_delta": r.get("net_sales_delta"),
            "net_sales_delta_pct": r.get("net_sales_delta_pct"),
            "comparison_available": r.get("comparison_available"),
            "comparison_note": r.get("comparison_note"),
            "source_as_of_date": r.get("as_of_date"),
            "data_policy": "latest_on_or_before_report_date",
        }
        for r in monthly_top_mtgt_customers_by_channel
    ]

    top_mtgt_customers_by_channel_map: Dict[str, List[dict]] = {}
    for row in top_mtgt_customers_by_channel:
        channel_group = str(row.get("channel_group") or "").upper()
        if not channel_group:
            continue
        top_mtgt_customers_by_channel_map.setdefault(channel_group, []).append(row)

    affiliate_cost_summary = find_total(affiliate_cost_by_channel) or {
        "affiliate_cost_wtd": 0,
        "affiliate_orders_wtd": 0,
        "affiliate_cost_mtd": 0,
        "affiliate_orders_mtd": 0,
    }

    package = {
        "metadata": {
            "report_type": PACKAGE_TYPE,
            "report_title": "CEO Growth Report - Week & Month",
            "package_version": PACKAGE_VERSION,
            "package_version_int": PACKAGE_VERSION_INT,
            "report_date": report_date.isoformat(),
            "current_week_start": current_week_start.isoformat(),
            "current_week_end": report_date.isoformat(),
            "previous_week_start": previous_week_start.isoformat(),
            "previous_week_end": previous_week_end.isoformat(),
            "current_month_start": current_month_start.isoformat(),
            "current_period_end": report_date.isoformat(),
            "previous_month_start": previous_month_start.isoformat(),
            "previous_period_end": previous_month_end.isoformat(),
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "scope": "Báo cáo tăng trưởng tuần/tháng, không thay thế báo cáo ngày.",
        },
        "data_quality": {
            "status": "FAIL" if errors else ("WARNING" if warnings else "PASS"),
            "can_send": len(errors) == 0,
            "errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "notes": sorted(set(notes)),
            "weekly_comparison_available": weekly_comparison_available,
            "monthly_comparison_available": monthly_comparison_available,
            "previous_monthly_daily_trend_available": monthly_daily_coverage["has_previous_daily_total_series"],
            "previous_weekly_daily_trend_available": weekly_daily_coverage["has_previous_daily_total_series"],
            "monthly_daily_coverage": monthly_daily_coverage,
            "weekly_daily_coverage": weekly_daily_coverage,
            "profit_pie_chart_usable": profit_pie_chart_usable,
        },

        "weekly_summary": weekly_summary,
        "weekly_comparison_vs_previous_week_same_days": weekly_comparison,
        "weekly_comparison_by_channel": [
            r for r in weekly_comparison_rows
            if str(r.get("channel", "")).upper() != "TOTAL"
        ],
        "weekly_daily_trend": weekly_daily_trend,
        "previous_weekly_daily_trend": previous_weekly_daily_trend,
        "weekly_total_sales_trend_current_vs_previous": weekly_total_sales_trend_current_vs_previous,
        "weekly_channel_sales_trend_current_vs_previous": weekly_channel_sales_trend_current_vs_previous,
        "weekly_scorecard_by_channel": weekly_scorecard_by_channel,
        "cost_breakdown_weekly": cost_breakdown_weekly,

        "monthly_summary": monthly_summary,
        "monthly_comparison_vs_previous_month_same_period": monthly_comparison,
        "monthly_comparison_by_channel": [
            r for r in monthly_comparison_rows
            if str(r.get("channel", "")).upper() != "TOTAL"
        ],
        "monthly_daily_trend": monthly_daily_trend,
        "previous_monthly_daily_trend": previous_monthly_daily_trend,
        "monthly_total_sales_trend_current_vs_previous": monthly_total_sales_trend_current_vs_previous,
        "monthly_channel_sales_trend_current_vs_previous": monthly_channel_sales_trend_current_vs_previous,
        "monthly_scorecard_by_channel": monthly_scorecard_by_channel,
        "monthly_target_forecast": monthly_target_forecast,
        "monthly_target_forecast_by_channel": [
            r for r in monthly_target_forecast_rows
            if str(r.get("channel", "")).upper() != "TOTAL"
        ],
        "cost_breakdown_monthly": cost_breakdown_monthly,

        "monthly_top_sku_sales_level": monthly_top_sku_sales_level,
        "monthly_top_sku_by_net_sales": monthly_top_sku_by_net_sales,
        "monthly_top_sku_by_channel": monthly_top_sku_by_channel,

        "monthly_channel_sales_share_chart": monthly_channel_sales_share_chart,
        "monthly_channel_profit_share_chart": monthly_channel_profit_share_chart,
        "monthly_total_sales_trend_chart": monthly_total_sales_trend_chart,
        "monthly_channel_sales_trend_chart": monthly_channel_sales_trend_chart,

        "monthly_top_mtgt_customers": monthly_top_mtgt_customers,
        "monthly_top_mtgt_customers_by_channel": monthly_top_mtgt_customers_by_channel,
        "affiliate_cost_by_channel": affiliate_cost_by_channel,
        "affiliate_cost_summary": affiliate_cost_summary,

        "chart_data": {
            "weekly_net_sales_trend": weekly_net_sales_trend,
            "weekly_total_net_sales_current_vs_previous": weekly_total_two_line_net_sales,
            "weekly_channel_net_sales_current_vs_previous": weekly_channel_two_line_net_sales,
            "monthly_net_sales_trend": monthly_net_sales_trend,
            "monthly_channel_sales_lines": monthly_channel_sales_lines,
            "monthly_total_net_sales_current_vs_previous": monthly_total_two_line_net_sales,
            "monthly_channel_net_sales_current_vs_previous": monthly_channel_two_line_net_sales,
            "monthly_sales_share_pie": monthly_sales_share_pie,
            "monthly_profit_share_or_bar": monthly_profit_share_or_bar,
            "monthly_profit_pie_chart_usable": profit_pie_chart_usable,
            "top_mtgt_customers": top_mtgt_customers,
            "top_mtgt_customers_by_channel": top_mtgt_customers_by_channel,
            "top_mtgt_customers_by_channel_map": top_mtgt_customers_by_channel_map,
            "top_mtgt_customers_gt": top_mtgt_customers_by_channel_map.get("GT", []),
            "top_mtgt_customers_mt": top_mtgt_customers_by_channel_map.get("MT", []),
            "affiliate_cost_by_channel": affiliate_cost_by_channel,
            "affiliate_cost_summary": affiliate_cost_summary,
        },

        "metric_rules": {
            "wtd": "Từ thứ Hai của tuần hiện tại đến ngày báo cáo.",
            "previous_wtd": "Cùng số ngày của tuần trước.",
            "mtd": "Từ ngày 01 của tháng hiện tại đến ngày báo cáo.",
            "previous_mtd": "Cùng kỳ tháng trước.",
            "two_line_monthly_chart": (
                "Dùng monthly_total_sales_trend_current_vs_previous để vẽ 2 đường: "
                "current_net_sales và previous_net_sales theo day_no."
            ),
            "two_line_weekly_chart": (
                "Dùng weekly_total_sales_trend_current_vs_previous để vẽ 2 đường: "
                "current_net_sales và previous_net_sales theo day_no."
            ),
            "two_line_channel_month_chart": (
                "Dùng monthly_channel_sales_trend_current_vs_previous để vẽ 2 đường "
                "theo từng kênh: current_net_sales và previous_net_sales theo day_no."
            ),
            "two_line_channel_week_chart": (
                "Dùng weekly_channel_sales_trend_current_vs_previous để vẽ 2 đường "
                "theo từng kênh: current_net_sales và previous_net_sales theo day_no."
            ),
            "profit_pie_rule": (
                "Chỉ dùng pie chart lợi nhuận nếu tất cả profit_mtd theo kênh "
                "không âm và tổng lợi nhuận dương. Nếu có kênh âm, dùng bar chart."
            ),
            "top_mtgt_customer_channel_rule": (
                "Sheet GT dùng chart_data.top_mtgt_customers_gt; sheet MT dùng "
                "chart_data.top_mtgt_customers_mt. Không lọc lại theo exact report_date; "
                "nguồn này đã được phép dùng latest_on_or_before_report_date."
            ),
            "affiliate_rule": (
                "Shopee affiliate lấy từ shopee.orders.affiliate_amount và đã nằm trong "
                "platform_fees hiện tại. TikTok affiliate/commission lấy từ "
                "tiktok.orders_pnl.commission_amount và được đưa riêng để báo cáo tuần/tháng."
            ),
        },

        "report_rendering_policy": {
            "output": "HTML Gmail-ready only.",
            "must_not_show": [
                "source_map",
                "metric_rules",
                "report_rendering_policy",
                "database view names",
                "markdown code fence",
                "inventory",
                "DOS",
                "sample orders",
            ],
            "chart_requirements": [
                "Pie/donut chart tỷ trọng Net Sales MTD theo kênh.",
                "Profit pie chart chỉ khi không có kênh profit âm; nếu có âm thì dùng bar chart.",
                "Line chart 2 đường tổng Net Sales tháng: tháng hiện tại vs cùng kỳ tháng trước.",
                "Line chart 2 đường tổng Net Sales tuần: tuần hiện tại vs cùng kỳ tuần trước.",
                "Line chart 2 đường theo từng kênh tháng: tháng hiện tại vs cùng kỳ tháng trước.",
                "Line chart 2 đường theo từng kênh tuần: tuần hiện tại vs cùng kỳ tuần trước.",
                "Top 5 khách hàng MT-GT theo tháng và so với cùng kỳ tháng trước nếu có dữ liệu.",
                "Sheet GT/MT phải lấy top khách hàng từ chart_data.top_mtgt_customers_gt và chart_data.top_mtgt_customers_mt; không hiển thị 0 khi danh sách này có dòng.",
                "Hiển thị affiliate/commission Shopee và TikTok từ chart_data.affiliate_cost_by_channel như một block chi phí riêng, không cộng trùng Shopee vào platform_fees.",
            ],
        },
    }

    return clean_value(package)


# ============================================================
# Markdown summary
# ============================================================

def build_md(package: Dict[str, Any]) -> str:
    meta = package["metadata"]
    dq = package["data_quality"]
    ws = package.get("weekly_summary") or {}
    ms = package.get("monthly_summary") or {}
    mf = package.get("monthly_target_forecast") or {}

    lines = [
        f"# {meta['report_title']} ({meta['report_date']})",
        "",
        "## Data quality",
        f"- Status: **{dq['status']}**",
        f"- Can send: **{dq['can_send']}**",
        f"- Weekly comparison available: **{dq['weekly_comparison_available']}**",
        f"- Monthly comparison available: **{dq['monthly_comparison_available']}**",
        f"- Previous weekly daily trend available: **{dq['previous_weekly_daily_trend_available']}**",
        f"- Previous monthly daily trend available: **{dq['previous_monthly_daily_trend_available']}**",
        f"- Profit pie chart usable: **{dq['profit_pie_chart_usable']}**",
    ]

    if dq["errors"]:
        lines += ["", "### Errors"] + [f"- {x}" for x in dq["errors"]]
    if dq["warnings"]:
        lines += ["", "### Warnings"] + [f"- {x}" for x in dq["warnings"]]
    if dq["notes"]:
        lines += ["", "### Notes"] + [f"- {x}" for x in dq["notes"]]

    lines += [
        "",
        "## Weekly summary",
        f"- Net Sales WTD: **{money(ws.get('net_sales_wtd'))}**",
        f"- Profit WTD: **{money(ws.get('profit_wtd'))}**",
        f"- Profit Margin WTD: **{pct(ws.get('profit_margin_pct_wtd'))}**",
        "",
        "## Monthly summary",
        f"- Net Sales MTD: **{money(ms.get('net_sales_mtd'))}**",
        f"- Profit MTD: **{money(ms.get('profit_mtd'))}**",
        f"- Profit Margin MTD: **{pct(ms.get('profit_margin_pct_mtd'))}**",
        f"- Forecast Net Sales month end: **{money(mf.get('forecast_net_sales_month_end'))}**",
        "",
        "## Important v3.3 blocks",
    ]

    for key in [
        "weekly_total_sales_trend_current_vs_previous",
        "weekly_channel_sales_trend_current_vs_previous",
        "monthly_total_sales_trend_current_vs_previous",
        "monthly_channel_sales_trend_current_vs_previous",
        "monthly_channel_sales_share_chart",
        "monthly_channel_profit_share_chart",
        "monthly_top_mtgt_customers",
        "chart_data",
    ]:
        val = package.get(key)
        if isinstance(val, list):
            lines.append(f"- {key}: {len(val)} rows")
        elif isinstance(val, dict):
            lines.append(f"- {key}: object")
        else:
            lines.append(f"- {key}: {val}")

    lines += [
        "",
        "> File .md này chỉ để kiểm nhanh. Khi gửi agent tạo báo cáo, hãy gửi file .json chi tiết.",
    ]

    return "\n".join(lines) + "\n"


# ============================================================
# Store DB best effort
# ============================================================

def store_package_best_effort(
    report_date: str,
    package: Dict[str, Any],
    md: str,
    json_path: str,
    md_path: str,
) -> None:
    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not relation_exists(cur, "control_tower", "report_packages"):
                print("WARNING: control_tower.report_packages does not exist; skip --store-db")
                return

            cur.execute("""
                SELECT column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE table_schema='control_tower'
                  AND table_name='report_packages'
            """)
            colmap = {r["column_name"]: r for r in cur.fetchall()}
            values: Dict[str, Any] = {}

            def add(names: Iterable[str], value: Any) -> None:
                for name in names:
                    if name in colmap and name not in values:
                        values[name] = value
                        return

            add(["report_date", "package_date", "date"], report_date)
            add(["report_type", "package_type", "type"], PACKAGE_TYPE)

            def version_value_for_column(column_name: str):
                dtype = str(colmap[column_name]["data_type"]).lower()
                udt = str(colmap[column_name]["udt_name"]).lower()
                if "int" in dtype or udt in {"int2", "int4", "int8"}:
                    return PACKAGE_VERSION_INT
                return PACKAGE_VERSION

            for version_col in ["version", "package_version", "schema_version", "package_schema_version"]:
                if version_col in colmap and version_col not in values:
                    values[version_col] = version_value_for_column(version_col)

            package_json = json.dumps(package, ensure_ascii=False, default=json_default)
            add(["payload", "package_json", "package_data", "data", "json_payload", "content_json"], package_json)
            add(["markdown", "markdown_summary", "summary_md", "content_md"], md)
            add(["json_path", "package_path", "file_path"], json_path)
            add(["md_path", "markdown_path"], md_path)
            add(["checksum", "package_checksum", "sha256"], hashlib.sha256(package_json.encode("utf-8")).hexdigest())

            now = dt.datetime.now()
            add(["created_at", "generated_at"], now)
            add(["updated_at"], now)

            if not values:
                print("WARNING: no compatible columns found in report_packages; skip --store-db")
                return

            columns = list(values.keys())
            sql = (
                f"INSERT INTO control_tower.report_packages "
                f"({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})"
            )

            try:
                cur.execute(sql, [values[c] for c in columns])
                conn.commit()
                print("Stored package into control_tower.report_packages")
            except Exception as exc:
                conn.rollback()
                print(f"WARNING: could not store package into report_packages: {exc}")


# ============================================================
# Main
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    parser.add_argument("--out-dir", default="outbox/packages")
    parser.add_argument("--store-db", action="store_true")
    args = parser.parse_args()

    try:
        report_date = parse_date(args.date).isoformat()
    except ValueError:
        print("ERROR: --date must be YYYY-MM-DD", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    package = build_package(report_date)

    json_path = out_dir / f"ceo_growth_package_v3_3_week_month_{report_date}.json"
    md_path = out_dir / f"ceo_growth_package_v3_3_week_month_{report_date}.md"

    json_text = json.dumps(package, ensure_ascii=False, indent=2, default=json_default)
    json_path.write_text(json_text, encoding="utf-8")

    md_text = build_md(package)
    md_path.write_text(md_text, encoding="utf-8")

    if args.store_db:
        store_package_best_effort(report_date, package, md_text, str(json_path), str(md_path))

    dq = package["data_quality"]

    print(f"JSON package: {json_path}")
    print(f"Markdown summary: {md_path}")
    print(f"Data quality: {dq['status']}")
    print(f"Previous monthly daily trend available: {dq['previous_monthly_daily_trend_available']}")
    print(f"Previous weekly daily trend available: {dq['previous_weekly_daily_trend_available']}")

    if dq["warnings"]:
        print("Warnings:")
        for w in dq["warnings"]:
            print(f"- {w}")

    if dq["errors"]:
        print("Errors:")
        for e in dq["errors"]:
            print(f"- {e}")

    return 0 if dq["can_send"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
