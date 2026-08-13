#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Send daily LPM sales scorecard emails.

Production rules:
- REPORT_DATE defaults to yesterday.
- Recipients with can_view_profit=true or recipient_type='ceo' receive the full
  table including cost/revenue detail columns, matching the CEO Excel form.
- Other staff receive the sales table plus cumulative ads ratio, cumulative ads cost,
  and booking columns.
- Booking rule v6: MT, GT, DIGITAL, and SHOPEE booking are 0. TikTok booking uses only the actual booking_fee already loaded in tiktok.orders_pnl.
- Email scope controls which rows are included: FULL, MT, GT, ECOM, DIGITAL,
  SHOPEE, TIKTOK.
- DRY_RUN=1 writes HTML previews to outbox instead of sending SMTP.
"""
from __future__ import annotations

import html
import os
import re
import smtplib
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any, Dict, Iterable, List

import psycopg2
import psycopg2.extras


DB_CONFIG = {
    "dbname": os.getenv("ETL_DB_NAME") or os.getenv("DB_NAME") or "DuLieu",
    "user": os.getenv("ETL_DB_USER") or os.getenv("DB_USER") or "postgres",
    "password": os.getenv("ETL_DB_PASSWORD") or os.getenv("DB_PASSWORD") or "",
    "host": os.getenv("ETL_DB_HOST") or os.getenv("DB_HOST") or "localhost",
    "port": int(os.getenv("ETL_DB_PORT") or os.getenv("DB_PORT") or "5433"),
}

OUTBOX = Path(os.getenv("OUTBOX_DIR", "outbox"))
MAIN_CHANNELS = {"MT", "GT", "DIGITAL", "ECOM"}
CHILD_CHANNELS = {"SHOPEE", "TIKTOK"}
EMPLOYEE_DETAIL_SCOPES = {"FULL", "MT", "GT"}
FIXED_BOOKING_TOTAL_POOL = 0
FIXED_BOOKING_CHANNEL_COUNT = 5
FIXED_BOOKING_PER_CHANNEL = FIXED_BOOKING_TOTAL_POOL // FIXED_BOOKING_CHANNEL_COUNT
TIKTOK_EXTRA_FIXED_BOOKING = 0
FIXED_BOOKING_SQL = "0::numeric"
TIKTOK_EXTRA_FIXED_BOOKING_SQL = "0::numeric"


class ReportError(RuntimeError):
    pass


def as_float(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return 0.0


def fetch_all(sql: str, params: Iterable[Any] | None = None) -> List[Dict[str, Any]]:
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, list(params or []))
            return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: Iterable[Any] | None = None) -> None:
    with psycopg2.connect(**DB_CONFIG) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, list(params or []))
            conn.commit()


def log_send(report_date: str, recipient: Dict[str, Any], status: str, error: str | None = None) -> None:
    try:
        execute(
            """
            INSERT INTO auth.email_report_send_logs(
                report_date, email, recipient_name, report_scope, recipient_type,
                status, error_message
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                report_date,
                recipient.get("email"),
                recipient.get("recipient_name"),
                recipient.get("report_scope"),
                recipient.get("recipient_type"),
                status,
                (error or "")[:2000] if error else None,
            ],
        )
    except Exception as exc:
        print(f"WARN cannot write email log for {recipient.get('email')}: {exc}", file=sys.stderr)


def get_report_date() -> str:
    raw = os.getenv("REPORT_DATE", "").strip()
    if raw:
        try:
            return str(datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError as exc:
            raise ReportError("REPORT_DATE must be YYYY-MM-DD") from exc
    return str(date.today() - timedelta(days=1))


def fmt_money(value: Any, dash_zero: bool = True) -> str:
    num = as_float(value)
    if dash_zero and abs(num) < 0.5:
        return "-"
    return f"{round(num):,}"


def fmt_percent(value: Any, dash_empty: bool = True) -> str:
    if value is None:
        return "-" if dash_empty else ""
    num = as_float(value)
    if dash_empty and abs(num) < 0.00001:
        return "-"
    if abs(num - round(num)) < 0.05:
        return f"{round(num):.0f}%"
    return f"{num:.1f}%"


def vi_date(report_date: str) -> str:
    d = datetime.strptime(report_date, "%Y-%m-%d").date()
    return d.strftime("%d/%m/%Y")


def month_label(report_date: str) -> str:
    d = datetime.strptime(report_date, "%Y-%m-%d").date()
    return f"tháng {d.month}"


def safe_file_part(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower() or "recipient"


def get_recipients() -> List[Dict[str, Any]]:
    return fetch_all(
        """
        SELECT
            email,
            recipient_name,
            report_scope,
            recipient_type,
            can_view_profit,
            is_enabled
        FROM auth.email_report_recipients
        WHERE is_enabled = TRUE
        ORDER BY id
        """
    )


def get_scorecard_rows(report_date: str) -> List[Dict[str, Any]]:
    sql = r"""
    WITH params AS (
        SELECT
            %s::date AS report_date,
            date_trunc('month', %s::date)::date AS month_start
    ),

    mtgt_base AS (
        SELECT
            s.channel_group AS channel_code,
            SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END)
                FILTER (WHERE s.sale_date = p.report_date) AS doanh_so_ngay,
            SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) AS tong_thuc_hien_toi_ngay,
            SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.quantity, 0) * COALESCE(s.unit_cogs, 0) ELSE 0 END) AS gia_von,
            SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.02 AS van_chuyen_phi_hoan,
            SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) * 0.15 AS backoffice,
            0::numeric AS live_in_house,
            0::numeric AS booking,
            0::numeric AS bao_bi_dong_goi,
            SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.net_revenue, 0) ELSE 0 END) AS tien_thu
        FROM mtgt.sales_lines s
        CROSS JOIN params p
        WHERE s.sale_date BETWEEN p.month_start AND p.report_date
          AND s.channel_group IN ('MT', 'GT')
        GROUP BY s.channel_group
    ),

    mtgt_ads AS (
        SELECT
            a.channel_group AS channel_code,
            SUM(COALESCE(a.cost_vnd, 0)) FILTER (WHERE a.ads_date = p.report_date) AS chi_phi_quang_cao_ngay,
            SUM(COALESCE(a.cost_vnd, 0)) AS quang_cao
        FROM mtgt.ads_costs a
        CROSS JOIN params p
        WHERE a.ads_date BETWEEN p.month_start AND p.report_date
          AND a.channel_group IN ('MT', 'GT')
        GROUP BY a.channel_group
    ),

    nhanh_item AS (
        SELECT
            oi.order_id,
            SUM(COALESCE(oi.quantity, 0) * COALESCE(oi.unit_cogs, 0)) AS gia_von,
            SUM(COALESCE(oi.quantity, 0)) AS tong_so_luong
        FROM nhanh.order_items oi
        GROUP BY oi.order_id
    ),

    digital_base AS (
        SELECT
            'DIGITAL' AS channel_code,
            SUM(COALESCE(o.order_value, 0)) FILTER (WHERE o.order_date = p.report_date) AS doanh_so_ngay,
            SUM(COALESCE(o.order_value, 0)) AS tong_thuc_hien_toi_ngay,
            SUM(COALESCE(i.gia_von, 0)) AS gia_von,
            -- v2.12 Nhanh: shipping_fee is already calculated by nhanh_etl.py as
            -- "Phí vận chuyển" - "Phí ship báo khách" plus 25,000 for exact status "Đã hoàn".
            SUM(COALESCE(o.shipping_fee, 0)) AS van_chuyen_phi_hoan,
            -- v2.12 Nhanh: no BO for exact statuses: Đã hủy, Hệ Thống hủy, Đã hoàn, Khách hủy.
            SUM(
                CASE
                    WHEN TRIM(COALESCE(o.status, '')) IN ('Đã hủy', 'Hệ Thống hủy', 'Đã hoàn', 'Khách hủy')
                    THEN 0
                    ELSE GREATEST(COALESCE(o.order_value, 0), 0) * 0.15
                END
            ) AS backoffice,
            0::numeric AS live_in_house,
            0::numeric AS booking,
            SUM(COALESCE(i.tong_so_luong, 0) * 2000) AS bao_bi_dong_goi,
            SUM(COALESCE(o.order_value, 0)) AS tien_thu
        FROM nhanh.orders o
        CROSS JOIN params p
        LEFT JOIN nhanh_item i ON i.order_id = o.id
        WHERE o.order_date BETWEEN p.month_start AND p.report_date
    ),

    digital_ads AS (
        SELECT
            'DIGITAL' AS channel_code,
            SUM(COALESCE(a.cost_vnd_vat, 0)) FILTER (WHERE a.ads_date = p.report_date) AS chi_phi_quang_cao_ngay,
            SUM(COALESCE(a.cost_vnd_vat, 0)) AS quang_cao
        FROM nhanh.ads_costs a
        CROSS JOIN params p
        WHERE a.ads_date BETWEEN p.month_start AND p.report_date
    ),

    shopee_item AS (
        SELECT
            oi.order_id,
            SUM(
                CASE
                    WHEN COALESCE(o.is_cancelled, false) THEN 0
                    ELSE COALESCE(oi.quantity, 0) * COALESCE(oi.unit_cogs, 0)
                END
            ) AS gia_von,
            SUM(
                CASE
                    WHEN COALESCE(o.is_cancelled, false) THEN 0
                    ELSE COALESCE(oi.quantity, 0)
                END
            ) AS tong_so_luong
        FROM shopee.order_items oi
        JOIN shopee.orders o ON o.id = oi.order_id
        GROUP BY oi.order_id
    ),

    shopee_order AS (
        SELECT
            o.id,
            o.order_date,
            CASE WHEN COALESCE(o.is_cancelled, false) THEN 0 ELSE COALESCE(o.seller_revenue, 0) END AS doanh_so,
            COALESCE(i.gia_von, 0) AS gia_von,
            COALESCE(o.return_fee, 0) AS van_chuyen_phi_hoan,
            COALESCE(i.tong_so_luong, 0) * 2000 AS bao_bi_dong_goi,
            CASE WHEN COALESCE(o.payout_actual, 0) <> 0 THEN COALESCE(o.payout_actual, 0) ELSE COALESCE(o.est_payout, 0) END AS tien_thu
        FROM shopee.orders o
        LEFT JOIN shopee_item i ON i.order_id = o.id
    ),

    shopee_base AS (
        SELECT
            'SHOPEE' AS channel_code,
            SUM(doanh_so) FILTER (WHERE order_date = p.report_date) AS doanh_so_ngay,
            SUM(doanh_so) AS tong_thuc_hien_toi_ngay,
            SUM(gia_von) AS gia_von,
            SUM(van_chuyen_phi_hoan) AS van_chuyen_phi_hoan,
            GREATEST(SUM(tien_thu), 0) * 0.15 AS backoffice,
            0::numeric AS live_in_house,
            0::numeric AS booking,
            SUM(bao_bi_dong_goi) AS bao_bi_dong_goi,
            SUM(tien_thu) AS tien_thu
        FROM shopee_order
        CROSS JOIN params p
        WHERE order_date BETWEEN p.month_start AND p.report_date
    ),

    shopee_ads AS (
        SELECT
            'SHOPEE' AS channel_code,
            SUM(COALESCE(a.cost_vnd_vat, 0)) FILTER (WHERE a.ads_date = p.report_date) AS chi_phi_quang_cao_ngay,
            SUM(COALESCE(a.cost_vnd_vat, 0)) AS quang_cao
        FROM shopee.ads_costs a
        CROSS JOIN params p
        WHERE a.ads_date BETWEEN p.month_start AND p.report_date
    ),

    tiktok_order AS (
        SELECT
            o.order_date,
            CASE WHEN COALESCE(o.is_cancelled, false) THEN 0 ELSE COALESCE(o.fee_base_revenue, 0) END AS doanh_so,
            COALESCE(o.cogs_amount, 0) AS gia_von,
            COALESCE(o.return_shipping_fee, 0) AS van_chuyen_phi_hoan,
            COALESCE(o.backoffice_cost, 0) AS backoffice,
            COALESCE(o.booking_fee, 0) AS booking,
            COALESCE(o.packaging_cost, 0) AS bao_bi_dong_goi,
            CASE
                WHEN COALESCE(o.settlement_paid, 0) + COALESCE(o.settlement_unpaid, 0) <> 0
                    THEN COALESCE(o.settlement_paid, 0) + COALESCE(o.settlement_unpaid, 0)
                ELSE COALESCE(o.estimated_payout, 0)
            END AS tien_thu
        FROM tiktok.orders_pnl o
    ),

    tiktok_base AS (
        SELECT
            'TIKTOK' AS channel_code,
            SUM(doanh_so) FILTER (WHERE order_date = p.report_date) AS doanh_so_ngay,
            SUM(doanh_so) AS tong_thuc_hien_toi_ngay,
            SUM(gia_von) AS gia_von,
            SUM(van_chuyen_phi_hoan) AS van_chuyen_phi_hoan,
            SUM(backoffice) AS backoffice,
            COALESCE((
                SELECT SUM(COALESCE(l.in_house_cost, 0))
                FROM tiktok.live_costs l
                CROSS JOIN params p2
                WHERE l.live_date BETWEEN p2.month_start AND p2.report_date
            ), 0) AS live_in_house,
            COALESCE(SUM(booking), 0) AS booking,
            SUM(bao_bi_dong_goi) AS bao_bi_dong_goi,
            SUM(tien_thu) AS tien_thu
        FROM tiktok_order
        CROSS JOIN params p
        WHERE order_date BETWEEN p.month_start AND p.report_date
    ),

    tiktok_ads AS (
        SELECT
            'TIKTOK' AS channel_code,
            COALESCE((SELECT SUM(COALESCE(a.cost_vnd_vat, 0)) FROM tiktok.ads_costs a, params p2 WHERE a.ads_date = p2.report_date), 0)
            + COALESCE((SELECT SUM(COALESCE(l.teaser_cost, 0)) FROM tiktok.live_costs l, params p2 WHERE l.live_date = p2.report_date), 0) AS chi_phi_quang_cao_ngay,
            COALESCE((SELECT SUM(COALESCE(a.cost_vnd_vat, 0)) FROM tiktok.ads_costs a, params p2 WHERE a.ads_date BETWEEN p2.month_start AND p2.report_date), 0)
            + COALESCE((SELECT SUM(COALESCE(l.teaser_cost, 0)) FROM tiktok.live_costs l, params p2 WHERE l.live_date BETWEEN p2.month_start AND p2.report_date), 0) AS quang_cao
    ),

    base_leaf AS (
        SELECT * FROM mtgt_base
        UNION ALL SELECT * FROM digital_base
        UNION ALL SELECT * FROM shopee_base
        UNION ALL SELECT * FROM tiktok_base
    ),

    ads_leaf AS (
        SELECT * FROM mtgt_ads
        UNION ALL SELECT * FROM digital_ads
        UNION ALL SELECT * FROM shopee_ads
        UNION ALL SELECT * FROM tiktok_ads
    ),

    leaf AS (
        SELECT
            b.channel_code,
            COALESCE(b.doanh_so_ngay, 0) AS doanh_so_ngay,
            COALESCE(b.tong_thuc_hien_toi_ngay, 0) AS tong_thuc_hien_toi_ngay,
            COALESCE(a.chi_phi_quang_cao_ngay, 0) AS chi_phi_quang_cao_ngay,
            COALESCE(a.quang_cao, 0) AS quang_cao,
            COALESCE(b.gia_von, 0) AS gia_von,
            COALESCE(b.van_chuyen_phi_hoan, 0) AS van_chuyen_phi_hoan,
            COALESCE(b.backoffice, 0) AS backoffice,
            COALESCE(b.live_in_house, 0) AS live_in_house,
            COALESCE(b.booking, 0) AS booking,
            COALESCE(b.bao_bi_dong_goi, 0) AS bao_bi_dong_goi,
            COALESCE(b.tien_thu, 0) AS tien_thu
        FROM base_leaf b
        LEFT JOIN ads_leaf a ON a.channel_code = b.channel_code
    ),

    ecom AS (
        SELECT
            'ECOM' AS channel_code,
            SUM(doanh_so_ngay) AS doanh_so_ngay,
            SUM(tong_thuc_hien_toi_ngay) AS tong_thuc_hien_toi_ngay,
            SUM(chi_phi_quang_cao_ngay) AS chi_phi_quang_cao_ngay,
            SUM(quang_cao) AS quang_cao,
            SUM(gia_von) AS gia_von,
            SUM(van_chuyen_phi_hoan) AS van_chuyen_phi_hoan,
            SUM(backoffice) AS backoffice,
            SUM(live_in_house) AS live_in_house,
            SUM(booking) AS booking,
            SUM(bao_bi_dong_goi) AS bao_bi_dong_goi,
            SUM(tien_thu) AS tien_thu
        FROM leaf
        WHERE channel_code IN ('SHOPEE', 'TIKTOK')
    ),

    all_rows AS (
        SELECT * FROM leaf
        UNION ALL SELECT * FROM ecom
    )

    SELECT
        t.display_order AS stt,
        t.channel_code,
        t.channel_name AS kenh,
        t.parent_channel,
        COALESCE(t.target_revenue, 0) AS chi_tieu_thang,
        COALESCE(r.tong_thuc_hien_toi_ngay, 0) AS tong_thuc_hien_toi_ngay,
        COALESCE(r.doanh_so_ngay, 0) AS doanh_so_ngay,
        COALESCE(r.chi_phi_quang_cao_ngay, 0) AS chi_phi_quang_cao,
        COALESCE(r.gia_von, 0) AS gia_von,
        COALESCE(r.van_chuyen_phi_hoan, 0) AS van_chuyen_phi_hoan,
        COALESCE(r.backoffice, 0) AS backoffice,
        COALESCE(r.live_in_house, 0) AS live_in_house,
        COALESCE(r.booking, 0) AS booking,
        COALESCE(r.quang_cao, 0) AS quang_cao,
        COALESCE(r.bao_bi_dong_goi, 0) AS bao_bi_dong_goi,
        COALESCE(r.tien_thu, 0) AS tien_thu,
        COALESCE(r.tien_thu, 0)
            - COALESCE(r.gia_von, 0)
            - COALESCE(r.van_chuyen_phi_hoan, 0)
            - COALESCE(r.backoffice, 0)
            - COALESCE(r.live_in_house, 0)
            - COALESCE(r.booking, 0)
            - COALESCE(r.quang_cao, 0)
            - COALESCE(r.bao_bi_dong_goi, 0) AS ln_con_lai,
        CASE
            WHEN COALESCE(t.target_revenue, 0) = 0 THEN NULL
            ELSE ROUND(COALESCE(r.tong_thuc_hien_toi_ngay, 0) / t.target_revenue * 100, 1)
        END AS ty_le_thuc_hien,
        CASE
            WHEN COALESCE(r.doanh_so_ngay, 0) = 0 AND COALESCE(r.chi_phi_quang_cao_ngay, 0) > 0 THEN 100.0
            WHEN COALESCE(r.doanh_so_ngay, 0) = 0 THEN NULL
            ELSE ROUND(COALESCE(r.chi_phi_quang_cao_ngay, 0) / r.doanh_so_ngay * 100, 1)
        END AS ty_le_quang_cao,
        CASE
            WHEN COALESCE(r.tong_thuc_hien_toi_ngay, 0) = 0 AND COALESCE(r.quang_cao, 0) > 0 THEN 100.0
            WHEN COALESCE(r.tong_thuc_hien_toi_ngay, 0) = 0 THEN NULL
            ELSE ROUND(COALESCE(r.quang_cao, 0) / r.tong_thuc_hien_toi_ngay * 100, 1)
        END AS ty_le_quang_cao_luy_ke
    FROM ops.monthly_sales_targets t
    LEFT JOIN all_rows r ON r.channel_code = t.channel_code
    CROSS JOIN params p
    WHERE t.target_month = p.month_start
      AND t.is_enabled = TRUE
    ORDER BY t.display_order;
    """
    rows = fetch_all(sql, [report_date, report_date])
    if not rows:
        raise ReportError(f"No target rows found for month of {report_date}. Check ops.monthly_sales_targets.")
    return rows


def get_employee_sales_rows(report_date: str) -> List[Dict[str, Any]]:
    """Return sales-only MT/GT employee details for staff email reporting.

    This table intentionally excludes profit and cost columns. The target tables
    are the source of the full employee list, so employees with no sales still
    appear in the report.
    """
    sql = r"""
    WITH table_check AS (
        SELECT
            to_regclass('ops.sales_employees') IS NOT NULL AS has_employees,
            to_regclass('ops.monthly_employee_sales_targets') IS NOT NULL AS has_targets
    ),

    params AS (
        SELECT
            %s::date AS report_date,
            date_trunc('month', %s::date)::date AS month_start
    ),

    actual AS (
        SELECT
            s.channel_group AS channel_code,
            COALESCE(NULLIF(BTRIM(s.employee_code), ''), 'UNASSIGNED') AS employee_code,
            UPPER(COALESCE(NULLIF(BTRIM(s.employee_code), ''), 'UNASSIGNED')) AS employee_code_key,
            COALESCE(NULLIF(s.employee_name, ''), 'Chưa gán nhân viên') AS employee_name,
            SUM(
                CASE
                    WHEN s.line_type <> 'CVC' AND s.sale_date = p.report_date
                    THEN COALESCE(s.net_revenue, 0)
                    ELSE 0
                END
            ) AS daily_revenue,
            SUM(
                CASE
                    WHEN s.line_type <> 'CVC'
                    THEN COALESCE(s.net_revenue, 0)
                    ELSE 0
                END
            ) AS mtd_revenue,
            COUNT(DISTINCT s.document_no) FILTER (
                WHERE s.line_type <> 'CVC'
                  AND s.sale_date = p.report_date
            ) AS daily_orders,
            COUNT(DISTINCT s.document_no) FILTER (
                WHERE s.line_type <> 'CVC'
            ) AS mtd_orders,
            SUM(
                CASE
                    WHEN s.line_type <> 'CVC'
                    THEN COALESCE(s.quantity, 0)
                    ELSE 0
                END
            ) AS mtd_quantity
        FROM mtgt.sales_lines s
        CROSS JOIN params p
        CROSS JOIN table_check tc
        WHERE tc.has_employees
          AND tc.has_targets
          AND s.sale_date BETWEEN p.month_start AND p.report_date
          AND s.channel_group IN ('MT', 'GT')
        GROUP BY
            s.channel_group,
            COALESCE(NULLIF(BTRIM(s.employee_code), ''), 'UNASSIGNED'),
            UPPER(COALESCE(NULLIF(BTRIM(s.employee_code), ''), 'UNASSIGNED')),
            COALESCE(NULLIF(s.employee_name, ''), 'Chưa gán nhân viên')
    ),

    target_base AS (
        SELECT
            e.channel_code,
            BTRIM(e.employee_code) AS employee_code,
            UPPER(BTRIM(e.employee_code)) AS employee_code_key,
            e.employee_name,
            COALESCE(e.is_temporary_code, false) AS is_temporary_code,
            COALESCE(t.target_revenue, 0) AS target_revenue
        FROM ops.monthly_employee_sales_targets t
        JOIN ops.sales_employees e ON e.employee_id = t.employee_id
        CROSS JOIN params p
        CROSS JOIN table_check tc
        WHERE tc.has_employees
          AND tc.has_targets
          AND t.target_month = p.month_start
          AND t.is_enabled = true
          AND e.is_enabled = true
          AND e.channel_code IN ('MT', 'GT')
    ),

    joined AS (
        SELECT
            COALESCE(tb.channel_code, a.channel_code) AS channel_code,
            COALESCE(tb.employee_code, a.employee_code) AS employee_code,
            COALESCE(tb.employee_name, a.employee_name) AS employee_name,
            COALESCE(tb.is_temporary_code, false) AS is_temporary_code,
            COALESCE(tb.target_revenue, 0) AS target_revenue,
            COALESCE(a.daily_revenue, 0) AS daily_revenue,
            COALESCE(a.mtd_revenue, 0) AS mtd_revenue,
            COALESCE(a.daily_orders, 0) AS daily_orders,
            COALESCE(a.mtd_orders, 0) AS mtd_orders,
            COALESCE(a.mtd_quantity, 0) AS mtd_quantity,
            CASE
                WHEN a.employee_code IS NULL THEN 'TARGET_NO_SALES'
                WHEN tb.employee_code IS NULL THEN 'SALES_NO_TARGET'
                WHEN a.employee_code = 'UNASSIGNED' THEN 'UNASSIGNED_SALES'
                ELSE 'OK'
            END AS mapping_status
        FROM target_base tb
        FULL OUTER JOIN actual a
          ON a.channel_code = tb.channel_code
         AND a.employee_code_key = tb.employee_code_key
    ),

    ranked AS (
        SELECT
            j.*,
            SUM(j.mtd_revenue) OVER (PARTITION BY j.channel_code) AS channel_mtd_revenue,
            RANK() OVER (
                PARTITION BY j.channel_code
                ORDER BY j.mtd_revenue DESC, j.employee_name
            ) AS rank_in_channel
        FROM joined j
    )

    SELECT
        channel_code,
        rank_in_channel,
        employee_code,
        employee_name,
        is_temporary_code,
        target_revenue,
        mtd_revenue,
        daily_revenue,
        CASE
            WHEN target_revenue = 0 THEN NULL
            ELSE ROUND(mtd_revenue / NULLIF(target_revenue, 0) * 100, 1)
        END AS achievement_pct,
        daily_orders,
        mtd_orders,
        mtd_quantity,
        CASE
            WHEN channel_mtd_revenue = 0 THEN NULL
            ELSE ROUND(mtd_revenue / NULLIF(channel_mtd_revenue, 0) * 100, 1)
        END AS channel_contribution_pct,
        mapping_status,
        CASE
            WHEN mapping_status = 'TARGET_NO_SALES' THEN 'Có chỉ tiêu nhưng chưa có doanh số'
            WHEN mapping_status = 'SALES_NO_TARGET' THEN 'Có doanh số nhưng chưa có chỉ tiêu'
            WHEN mapping_status = 'UNASSIGNED_SALES' THEN 'Doanh số thiếu mã nhân viên'
            WHEN is_temporary_code THEN 'Đang dùng mã nhân viên tạm'
            WHEN target_revenue = 0 THEN 'Chưa giao chỉ tiêu'
            WHEN mtd_revenue >= target_revenue THEN 'Đạt/vượt chỉ tiêu'
            WHEN daily_revenue = 0 THEN 'Không phát sinh doanh số ngày'
            ELSE 'Đang thực hiện'
        END AS status_note
    FROM ranked
    ORDER BY channel_code, rank_in_channel, employee_name;
    """
    try:
        return fetch_all(sql, [report_date, report_date])
    except Exception as exc:
        print(f"WARN cannot fetch employee sales rows: {exc}", file=sys.stderr)
        return []


def make_total_row(rows: List[Dict[str, Any]], detailed: bool) -> Dict[str, Any]:
    main = [r for r in rows if str(r.get("channel_code", "")).upper() in MAIN_CHANNELS]
    total: Dict[str, Any] = {
        "stt": "",
        "channel_code": "TOTAL",
        "kenh": "Tổng",
        "parent_channel": None,
    }
    sum_cols = [
        "chi_tieu_thang", "tong_thuc_hien_toi_ngay", "doanh_so_ngay", "chi_phi_quang_cao",
        "gia_von", "van_chuyen_phi_hoan", "backoffice", "live_in_house", "booking", "quang_cao",
        "bao_bi_dong_goi", "tien_thu", "ln_con_lai",
    ]
    for col in sum_cols:
        total[col] = sum(as_float(r.get(col)) for r in main)
    total["ty_le_thuc_hien"] = None if as_float(total["chi_tieu_thang"]) == 0 else round(as_float(total["tong_thuc_hien_toi_ngay"]) / as_float(total["chi_tieu_thang"]) * 100, 1)
    total["ty_le_quang_cao"] = None if as_float(total["doanh_so_ngay"]) == 0 else round(as_float(total["chi_phi_quang_cao"]) / as_float(total["doanh_so_ngay"]) * 100, 1)
    total["ty_le_quang_cao_luy_ke"] = None if as_float(total["tong_thuc_hien_toi_ngay"]) == 0 else round(as_float(total["quang_cao"]) / as_float(total["tong_thuc_hien_toi_ngay"]) * 100, 1)
    return total


def filter_rows_by_scope(rows: List[Dict[str, Any]], scope: str) -> List[Dict[str, Any]]:
    scope = str(scope or "").upper()
    if scope == "FULL":
        ordered = [r for r in rows if str(r.get("channel_code", "")).upper() in {"MT", "GT", "DIGITAL", "ECOM", "SHOPEE", "TIKTOK"}]
        ordered.append(make_total_row(ordered, detailed=True))
        return ordered
    if scope == "MT":
        return [r for r in rows if str(r.get("channel_code", "")).upper() == "MT"]
    if scope == "GT":
        return [r for r in rows if str(r.get("channel_code", "")).upper() == "GT"]
    if scope == "DIGITAL":
        return [r for r in rows if str(r.get("channel_code", "")).upper() == "DIGITAL"]
    if scope == "ECOM":
        return [r for r in rows if str(r.get("channel_code", "")).upper() in {"ECOM", "SHOPEE", "TIKTOK"}]
    if scope == "SHOPEE":
        return [r for r in rows if str(r.get("channel_code", "")).upper() == "SHOPEE"]
    if scope == "TIKTOK":
        return [r for r in rows if str(r.get("channel_code", "")).upper() == "TIKTOK"]
    return []


def filter_employee_rows_by_scope(rows: List[Dict[str, Any]], scope: str) -> List[Dict[str, Any]]:
    scope = str(scope or "").upper()
    if scope == "FULL":
        return [r for r in rows if str(r.get("channel_code", "")).upper() in {"MT", "GT"}]
    if scope in {"MT", "GT"}:
        return [r for r in rows if str(r.get("channel_code", "")).upper() == scope]
    return []


def css_block() -> str:
    return """
    <style>
      body { font-family: Arial, Helvetica, sans-serif; font-size: 14px; color: #111; }
      .report-title { font-weight: bold; margin: 0 0 10px 0; }
      table.scorecard { border-collapse: collapse; width: auto; max-width: 100%; }
      table.scorecard th, table.scorecard td {
        border: 1px solid #222;
        padding: 5px 7px;
        text-align: right;
        vertical-align: middle;
        white-space: nowrap;
        font-weight: bold;
      }
      table.scorecard th { text-align: center; }
      .h-yellow { background: #ffff00; }
      .h-blue { background: #67d7ee; }
      .h-pink { background: #efb7ee; }
      .h-green { background: #dbead2; }
      .total-row td { background: #bfd0ff; color: #0066a8; font-weight: bold; }
      .child-row td { font-style: italic; font-weight: normal; }
      .left { text-align: left !important; }
      .center { text-align: center !important; }
      .note { color: #666; font-size: 12px; margin-top: 12px; }
      .sub-title { font-weight: bold; margin: 18px 0 8px 0; }
    </style>
    """


def render_header(report_date: str, detailed: bool) -> str:
    d = html.escape(vi_date(report_date))
    ml = html.escape(month_label(report_date))
    basic = f"""
      <tr>
        <th class="h-yellow" rowspan="2">STT</th>
        <th class="h-yellow" rowspan="2">Kênh</th>
        <th class="h-blue" rowspan="2">% thực<br>hiện DS<br>{ml}</th>
        <th class="h-blue" rowspan="2">Chỉ tiêu DS tháng<br>{ml.replace('tháng ', '')}</th>
        <th class="h-blue">Tổng thực hiện<br>tới</th>
        <th class="h-pink">Doanh số ngày</th>
        <th class="h-pink">Chi phí quảng<br>cáo</th>
        <th class="h-pink" rowspan="2">% quảng<br>cáo/ doanh<br>số ngày</th>
        <th class="h-pink" rowspan="2">% quảng cáo<br>lũy kế / tổng<br>doanh số</th>
    """
    if not detailed:
        basic += """
        <th class="h-pink" rowspan="2">Lũy kế<br>quảng cáo</th>
        <th class="h-green" rowspan="2">Booking /<br>phí cố định</th>
        """
    if detailed:
        detail = """
        <th class="h-green" colspan="9">Chi tiết chi phí - doanh thu</th>
      </tr>
      <tr>
        <th class="h-blue">{d}</th>
        <th class="h-pink">{d}</th>
        <th class="h-pink">{d}</th>
        <th class="h-green">Giá vốn hàng<br>bán + khuyến<br>mại</th>
        <th class="h-green">Vận chuyển<br>(MT,GT) - Phí<br>hoàn (Ecom)</th>
        <th class="h-green">Back office +<br>thuê ngoài</th>
        <th class="h-green">Live in house</th>
        <th class="h-green">Booking</th>
        <th class="h-green">Quảng cáo</th>
        <th class="h-green">Bao bì đóng gói</th>
        <th class="h-green">Tiền Thu từ khách<br>hoặc nhận từ sàn (dự<br>tính)</th>
        <th class="h-green">LN còn lại</th>
      </tr>
        """.format(d=d)
    else:
        detail = f"""
      </tr>
      <tr>
        <th class="h-blue">{d}</th>
        <th class="h-pink">{d}</th>
        <th class="h-pink">{d}</th>
      </tr>
        """
    return basic + detail


def render_rows(rows: List[Dict[str, Any]], detailed: bool) -> str:
    out: List[str] = []
    for r in rows:
        code = str(r.get("channel_code", "")).upper()
        tr_class = ""
        if code == "TOTAL":
            tr_class = " class=\"total-row\""
        elif code in CHILD_CHANNELS:
            tr_class = " class=\"child-row\""
        stt = "" if code in CHILD_CHANNELS or code == "TOTAL" else str(r.get("stt") or "")
        kenh = "Tổng" if code == "TOTAL" else str(r.get("kenh") or "")
        if code in CHILD_CHANNELS:
            kenh = "&nbsp;&nbsp;&nbsp;" + html.escape(kenh)
        else:
            kenh = html.escape(kenh)
        cells = [
            f"<td>{html.escape(stt)}</td>",
            f"<td class=\"left\">{kenh}</td>",
            f"<td>{html.escape(fmt_percent(r.get('ty_le_thuc_hien'), dash_empty=False))}</td>",
            f"<td>{html.escape(fmt_money(r.get('chi_tieu_thang'), dash_zero=False))}</td>",
            f"<td>{html.escape(fmt_money(r.get('tong_thuc_hien_toi_ngay'), dash_zero=False))}</td>",
            f"<td>{html.escape(fmt_money(r.get('doanh_so_ngay'), dash_zero=True))}</td>",
            f"<td>{html.escape(fmt_money(r.get('chi_phi_quang_cao'), dash_zero=True))}</td>",
            f"<td>{html.escape(fmt_percent(r.get('ty_le_quang_cao'), dash_empty=True))}</td>",
            f"<td>{html.escape(fmt_percent(r.get('ty_le_quang_cao_luy_ke'), dash_empty=True))}</td>",
        ]
        if not detailed:
            cells.append(f"<td>{html.escape(fmt_money(r.get('quang_cao'), dash_zero=True))}</td>")
            cells.append(f"<td>{html.escape(fmt_money(r.get('booking'), dash_zero=True))}</td>")
        if detailed:
            cells.extend([
                f"<td>{html.escape(fmt_money(r.get('gia_von'), dash_zero=True))}</td>",
                f"<td>{html.escape(fmt_money(r.get('van_chuyen_phi_hoan'), dash_zero=True))}</td>",
                f"<td>{html.escape(fmt_money(r.get('backoffice'), dash_zero=True))}</td>",
                f"<td>{html.escape(fmt_money(r.get('live_in_house'), dash_zero=True))}</td>",
                f"<td>{html.escape(fmt_money(r.get('booking'), dash_zero=True))}</td>",
                f"<td>{html.escape(fmt_money(r.get('quang_cao'), dash_zero=True))}</td>",
                f"<td>{html.escape(fmt_money(r.get('bao_bi_dong_goi'), dash_zero=True))}</td>",
                f"<td>{html.escape(fmt_money(r.get('tien_thu'), dash_zero=False))}</td>",
                f"<td>{html.escape(fmt_money(r.get('ln_con_lai'), dash_zero=False))}</td>",
            ])
        out.append(f"<tr{tr_class}>" + "".join(cells) + "</tr>")
    return "\n".join(out)


def render_employee_header() -> str:
    return """
      <tr>
        <th class="h-yellow">Kênh</th>
        <th class="h-yellow">Rank</th>
        <th class="h-yellow">Nhân viên</th>
        <th class="h-blue">Chỉ tiêu tháng</th>
        <th class="h-blue">Tổng thực hiện tới ngày</th>
        <th class="h-pink">Doanh số ngày</th>
        <th class="h-blue">% thực hiện</th>
        <th class="h-pink">Số đơn ngày</th>
        <th class="h-blue">Số đơn lũy kế</th>
        <th class="h-blue">Số lượng lũy kế</th>
        <th class="h-blue">Tỷ trọng trong kênh</th>
        <th class="h-green">Trạng thái</th>
      </tr>
    """


def render_employee_rows(rows: List[Dict[str, Any]]) -> str:
    out: List[str] = []
    for r in rows:
        status = str(r.get("status_note") or "")
        if r.get("is_temporary_code"):
            status = f"{status}; mã tạm" if status else "Đang dùng mã nhân viên tạm"
        cells = [
            f"<td class=\"center\">{html.escape(str(r.get('channel_code') or ''))}</td>",
            f"<td>{html.escape(str(r.get('rank_in_channel') or ''))}</td>",
            f"<td class=\"left\">{html.escape(str(r.get('employee_name') or ''))}</td>",
            f"<td>{html.escape(fmt_money(r.get('target_revenue'), dash_zero=False))}</td>",
            f"<td>{html.escape(fmt_money(r.get('mtd_revenue'), dash_zero=False))}</td>",
            f"<td>{html.escape(fmt_money(r.get('daily_revenue'), dash_zero=True))}</td>",
            f"<td>{html.escape(fmt_percent(r.get('achievement_pct'), dash_empty=True))}</td>",
            f"<td>{html.escape(fmt_money(r.get('daily_orders'), dash_zero=True))}</td>",
            f"<td>{html.escape(fmt_money(r.get('mtd_orders'), dash_zero=True))}</td>",
            f"<td>{html.escape(fmt_money(r.get('mtd_quantity'), dash_zero=True))}</td>",
            f"<td>{html.escape(fmt_percent(r.get('channel_contribution_pct'), dash_empty=True))}</td>",
            f"<td class=\"left\">{html.escape(status)}</td>",
        ]
        out.append("<tr>" + "".join(cells) + "</tr>")
    return "\n".join(out)


def render_employee_sales_section(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return ""
    return f"""
  <p class="sub-title">Chi tiết doanh số nhân viên MT/GT</p>
  <table class="scorecard">
    <thead>
      {render_employee_header()}
    </thead>
    <tbody>
      {render_employee_rows(rows)}
    </tbody>
  </table>
  <p class="note">Bảng nhân viên chỉ hiển thị doanh số/chỉ tiêu/số đơn/số lượng, không hiển thị chi phí hoặc lợi nhuận.</p>
"""


def render_email_html(
    report_date: str,
    recipient: Dict[str, Any],
    rows: List[Dict[str, Any]],
    detailed: bool,
    employee_rows: List[Dict[str, Any]] | None = None,
) -> str:
    title = "Báo cáo doanh số LPM"
    if detailed:
        title = "Báo cáo doanh số & chi phí LPM"
    employee_section = render_employee_sales_section(employee_rows or [])
    return f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
{css_block()}
</head>
<body>
  <p>Xin chào <b>{html.escape(str(recipient.get('recipient_name') or ''))}</b>,</p>
  <p class="report-title">{html.escape(title)} ngày {html.escape(vi_date(report_date))}</p>
  <table class="scorecard">
    <thead>
      {render_header(report_date, detailed)}
    </thead>
    <tbody>
      {render_rows(rows, detailed)}
    </tbody>
  </table>
  {employee_section}
  <p class="report-footer">Trân Trọng!</p>
</body>
</html>
"""


def send_email(to_email: str, subject: str, html_content: str) -> None:
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_password = os.getenv("SMTP_PASSWORD", "")
    from_email = os.getenv("SMTP_FROM_EMAIL", smtp_user)
    from_name = os.getenv("SMTP_FROM_NAME", "He thong bao cao LPM")

    if not smtp_user or not smtp_password:
        raise ReportError("Missing SMTP_USER or SMTP_PASSWORD")
    if not from_email:
        raise ReportError("Missing SMTP_FROM_EMAIL")

    msg = MIMEMultipart("alternative")
    msg["From"] = f"{from_name} <{from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(html_content, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port, timeout=60) as server:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.sendmail(from_email, [to_email], msg.as_string())


def main() -> int:
    report_date = get_report_date()
    dry_run = os.getenv("DRY_RUN", "0").strip().lower() in {"1", "true", "yes", "y"}

    recipients = get_recipients()
    if not recipients:
        print("No enabled recipients in auth.email_report_recipients")
        return 0

    score_rows = get_scorecard_rows(report_date)
    employee_sales_rows = get_employee_sales_rows(report_date)
    OUTBOX.mkdir(parents=True, exist_ok=True)

    sent_count = 0
    for rec in recipients:
        try:
            scope_rows = filter_rows_by_scope(score_rows, rec.get("report_scope"))
            if not scope_rows:
                print(f"SKIP {rec.get('email')}: no rows for scope={rec.get('report_scope')}")
                continue
            detailed = bool(rec.get("can_view_profit")) or str(rec.get("recipient_type", "")).lower() == "ceo"
            employee_scope_rows = filter_employee_rows_by_scope(employee_sales_rows, rec.get("report_scope"))
            html_body = render_email_html(report_date, rec, scope_rows, detailed, employee_scope_rows)
            subject = f"Bao cao doanh so LPM ngay {vi_date(report_date)}"
            if detailed:
                subject = f"Bao cao doanh so va chi phi LPM ngay {vi_date(report_date)}"

            if dry_run:
                path = OUTBOX / f"preview_{safe_file_part(str(rec.get('email')))}_{report_date}.html"
                path.write_text(html_body, encoding="utf-8")
                log_send(report_date, rec, "dry_run", None)
                print(f"DRY RUN wrote {path} scope={rec.get('report_scope')} detailed={detailed} rows={len(scope_rows)} employee_rows={len(employee_scope_rows)}")
            else:
                send_email(str(rec.get("email")), subject, html_body)
                log_send(report_date, rec, "success", None)
                print(f"SENT {rec.get('email')} scope={rec.get('report_scope')} detailed={detailed} rows={len(scope_rows)} employee_rows={len(employee_scope_rows)}")
            sent_count += 1
        except Exception as exc:
            log_send(report_date, rec, "failed", str(exc))
            print(f"ERROR {rec.get('email')}: {exc}", file=sys.stderr)
            continue

    print(f"DONE email reports for {report_date}: {sent_count} recipient(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())