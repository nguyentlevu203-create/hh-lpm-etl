#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build CEO Daily Cumulative package.

Mục tiêu:
- Báo cáo đúng 1 ngày và lũy kế từ đầu tháng đến ngày báo cáo.
- Không thay đổi ETL hiện tại; script này chỉ đọc DB và đóng package JSON/MD.
- Shopee/TikTok tính waterfall theo công thức CEO bằng các dữ liệu thực tế đã có.
- Chỉ số không có nguồn dữ liệu thì để None + ghi vào excluded_missing_metrics,
  không ép thành 0 và không trừ vào công thức.
- Nhanh.vn tách riêng theo từng giá trị nhanh.orders.label; không gộp thành một dòng DIGITAL/NHANH.
- Không cộng trùng dòng cha/con khi tính TOTAL.
"""

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


PACKAGE_TYPE = "CEO_DAILY_CUMULATIVE"
PACKAGE_VERSION = "v1.0"
PACKAGE_VERSION_INT = 10


# ============================================================
# Generic helpers
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


def month_start(d: dt.date) -> dt.date:
    return dt.date(d.year, d.month, 1)


def safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, decimal.Decimal):
        return float(value)
    try:
        return float(value)
    except Exception:
        return None


def nz(value: Any) -> float:
    v = safe_float(value)
    return 0.0 if v is None else v


def connect():
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(
        host=os.getenv("PGHOST", os.getenv("ETL_DB_HOST", os.getenv("DB_HOST", "localhost"))),
        port=os.getenv("PGPORT", os.getenv("ETL_DB_PORT", os.getenv("DB_PORT", "5433"))),
        dbname=os.getenv("PGDATABASE", os.getenv("ETL_DB_NAME", os.getenv("DB_NAME", "DuLieu"))),
        user=os.getenv("PGUSER", os.getenv("ETL_DB_USER", os.getenv("DB_USER", "postgres"))),
        password=os.getenv("PGPASSWORD", os.getenv("ETL_DB_PASSWORD", os.getenv("DB_PASSWORD", ""))),
    )


def relation_exists(cur, schema: str, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{name}",))
    row = cur.fetchone()
    return bool(row and row["ok"])


def get_columns(cur, schema: str, name: str) -> set[str]:
    if not relation_exists(cur, schema, name):
        return set()
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema=%s AND table_name=%s
        """,
        (schema, name),
    )
    return {str(r["column_name"]) for r in cur.fetchall()}


def pick_col(cols: set[str], candidates: Iterable[str]) -> Optional[str]:
    lower = {c.lower(): c for c in cols}
    for name in candidates:
        if name.lower() in lower:
            return lower[name.lower()]
    return None


def qident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


def num_expr(alias: str, col: Optional[str], default: str = "NULL::numeric") -> str:
    if not col:
        return default
    return f"{alias}.{qident(col)}"


def add_if_available(base: Optional[float], components: Dict[str, Optional[float]]) -> Tuple[Optional[float], List[str], List[str]]:
    """
    base - sum(available components).
    Missing components are skipped exactly as requested by owner.
    """
    if base is None:
        return None, [], list(components.keys())
    included: List[str] = []
    excluded: List[str] = []
    result = float(base)
    for name, value in components.items():
        if value is None:
            excluded.append(name)
            continue
        result -= float(value)
        included.append(name)
    return result, included, excluded


def stage_status(excluded: List[str]) -> str:
    return "AVAILABLE_PARTIAL" if excluded else "AVAILABLE_FULL"


def merge_excluded(*groups: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for group in groups:
        for item in group:
            if item not in seen:
                seen.add(item)
                out.append(item)
    return out


def row_with_waterfall(
    *,
    channel: str,
    subchannel: Optional[str],
    source_system: str,
    period_start: dt.date,
    period_end: dt.date,
    gross_sales: Optional[float],
    seller_funded_discount: Optional[float],
    platform_funded_discount: Optional[float],
    returns_refunds_revenue: Optional[float],
    vat: Optional[float],
    net_sales: Optional[float],
    cogs_total: Optional[float],
    sold_cogs: Optional[float],
    gift_promo_cogs: Optional[float],
    platform_fee: Optional[float],
    payment_fee: Optional[float],
    fulfillment_fee: Optional[float],
    packaging_cost: Optional[float],
    outbound_logistics: Optional[float],
    return_reverse_logistics: Optional[float],
    ads_cost: Optional[float],
    affiliate_commission: Optional[float],
    live_cost: Optional[float],
    booking_cost: Optional[float],
    kol_koc_creator_cost: Optional[float],
    content_trade_agency_cost: Optional[float],
    backoffice_cost: Optional[float],
    payroll: Optional[float],
    direct_overhead: Optional[float],
    central_opex: Optional[float],
    payout: Optional[float],
    orders: Optional[int],
    success_orders: Optional[int],
    cancelled_orders: Optional[int],
    quantity: Optional[float],
    metric_sources: Dict[str, Any],
    calculation_notes: List[str],
    explicit_missing: Optional[List[str]] = None,
) -> Dict[str, Any]:
    # GM1. If sold/gift split is unavailable but total COGS is available,
    # subtract cogs_total once and do not double count.
    if sold_cogs is not None or gift_promo_cogs is not None:
        gm1_components = {
            "sold_cogs": sold_cogs,
            "gift_promo_cogs": gift_promo_cogs,
        }
    else:
        gm1_components = {"cogs_total": cogs_total}
    gm1, gm1_included, gm1_excluded = add_if_available(net_sales, gm1_components)

    cm1_components = {
        "platform_fee": platform_fee,
        "payment_fee": payment_fee,
        "fulfillment_fee": fulfillment_fee,
        "packaging_cost": packaging_cost,
        "outbound_logistics": outbound_logistics,
        "return_reverse_logistics": return_reverse_logistics,
    }
    cm1, cm1_included, cm1_excluded = add_if_available(gm1, cm1_components)

    cm2_components = {
        "ads_cost": ads_cost,
        "affiliate_commission": affiliate_commission,
        "live_cost": live_cost,
        "booking_cost": booking_cost,
        "kol_koc_creator_cost": kol_koc_creator_cost,
        "content_trade_agency_cost": content_trade_agency_cost,
    }
    cm2, cm2_included, cm2_excluded = add_if_available(cm1, cm2_components)

    ebitda_components = {
        "backoffice_cost": backoffice_cost,
        "payroll": payroll,
        "direct_overhead": direct_overhead,
        "central_opex": central_opex,
    }
    ebitda_available, ebitda_included, ebitda_excluded = add_if_available(cm2, ebitda_components)

    missing = merge_excluded(
        explicit_missing or [],
        gm1_excluded,
        cm1_excluded,
        cm2_excluded,
        ebitda_excluded,
    )

    return clean_value({
        "channel": channel,
        "subchannel": subchannel,
        "source_system": source_system,
        "period_start": period_start,
        "period_end": period_end,
        "gross_sales": gross_sales,
        "seller_funded_discount": seller_funded_discount,
        "platform_funded_discount": platform_funded_discount,
        "returns_refunds_revenue": returns_refunds_revenue,
        "vat": vat,
        "net_sales": net_sales,
        "cogs_total": cogs_total,
        "sold_cogs": sold_cogs,
        "gift_promo_cogs": gift_promo_cogs,
        "gm1": gm1,
        "platform_fee": platform_fee,
        "payment_fee": payment_fee,
        "fulfillment_fee": fulfillment_fee,
        "packaging_cost": packaging_cost,
        "outbound_logistics": outbound_logistics,
        "return_reverse_logistics": return_reverse_logistics,
        "cm1": cm1,
        "ads_cost": ads_cost,
        "affiliate_commission": affiliate_commission,
        "live_cost": live_cost,
        "booking_cost": booking_cost,
        "kol_koc_creator_cost": kol_koc_creator_cost,
        "content_trade_agency_cost": content_trade_agency_cost,
        "cm2": cm2,
        "backoffice_cost": backoffice_cost,
        "payroll": payroll,
        "direct_overhead": direct_overhead,
        "central_opex": central_opex,
        "ebitda_available": ebitda_available,
        "payout": payout,
        "orders": orders,
        "success_orders": success_orders,
        "cancelled_orders": cancelled_orders,
        "quantity": quantity,
        "gm1_included_metrics": gm1_included,
        "cm1_included_metrics": cm1_included,
        "cm2_included_metrics": cm2_included,
        "ebitda_included_metrics": ebitda_included,
        "excluded_missing_metrics": missing,
        "calculation_status": "AVAILABLE_PARTIAL" if missing else "AVAILABLE_FULL",
        "gm1_status": stage_status(gm1_excluded),
        "cm1_status": stage_status(cm1_excluded),
        "cm2_status": stage_status(cm2_excluded),
        "ebitda_status": stage_status(ebitda_excluded),
        "metric_sources": metric_sources,
        "calculation_notes": calculation_notes,
    })


# ============================================================
# MT / GT
# ============================================================

def fetch_mtgt(cur, start_date: dt.date, end_date: dt.date) -> List[dict]:
    if not relation_exists(cur, "mtgt", "sales_lines"):
        return []

    cols = get_columns(cur, "mtgt", "sales_lines")
    gross_col = pick_col(cols, ["gross_sales", "gross_revenue", "doanh_so_ban", "gross_amount"])
    document_col = pick_col(cols, ["document_no", "document_number", "so_chung_tu"])
    quantity_col = pick_col(cols, ["quantity", "qty"])
    unit_cogs_col = pick_col(cols, ["unit_cogs", "gia_von"])

    gross_sql = (
        f"SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.{qident(gross_col)},0) ELSE 0 END)::numeric"
        if gross_col else "NULL::numeric"
    )
    docs_sql = (
        f"COUNT(DISTINCT s.{qident(document_col)})"
        if document_col else "COUNT(*)"
    )
    qty_sql = (
        f"SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.{qident(quantity_col)},0) ELSE 0 END)::numeric"
        if quantity_col else "NULL::numeric"
    )
    cogs_sql = (
        f"SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.{qident(quantity_col)},0) * COALESCE(s.{qident(unit_cogs_col)},0) ELSE 0 END)::numeric"
        if quantity_col and unit_cogs_col else "NULL::numeric"
    )

    cur.execute(
        f"""
        SELECT
            UPPER(s.channel_group)::text AS channel,
            {gross_sql} AS gross_sales,
            SUM(CASE WHEN s.line_type <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END)::numeric AS net_sales,
            {cogs_sql} AS cogs_total,
            {qty_sql} AS quantity,
            {docs_sql}::bigint AS orders
        FROM mtgt.sales_lines s
        WHERE s.sale_date BETWEEN %s AND %s
          AND UPPER(s.channel_group) IN ('MT','GT')
        GROUP BY UPPER(s.channel_group)
        ORDER BY UPPER(s.channel_group)
        """,
        (start_date, end_date),
    )
    base = {r["channel"]: dict(r) for r in cur.fetchall()}

    ads: Dict[str, float] = {}
    if relation_exists(cur, "mtgt", "ads_costs"):
        cur.execute(
            """
            SELECT UPPER(channel_group)::text AS channel,
                   SUM(COALESCE(cost_vnd,0))::numeric AS ads_cost
            FROM mtgt.ads_costs
            WHERE ads_date BETWEEN %s AND %s
              AND UPPER(channel_group) IN ('MT','GT')
            GROUP BY UPPER(channel_group)
            """,
            (start_date, end_date),
        )
        ads = {str(r["channel"]): nz(r["ads_cost"]) for r in cur.fetchall()}

    rows = []
    for channel in ("MT", "GT"):
        if channel not in base:
            continue
        r = base[channel]
        net = safe_float(r.get("net_sales"))
        cogs = safe_float(r.get("cogs_total"))
        # Preserve current management rules already used by report pipeline.
        shipping = None if net is None else net * 0.02
        backoffice = None if net is None else net * 0.15
        rows.append(row_with_waterfall(
            channel=channel,
            subchannel=None,
            source_system="MTGT",
            period_start=start_date,
            period_end=end_date,
            gross_sales=safe_float(r.get("gross_sales")),
            seller_funded_discount=None,
            platform_funded_discount=None,
            returns_refunds_revenue=None,
            vat=None,
            net_sales=net,
            cogs_total=cogs,
            sold_cogs=None,
            gift_promo_cogs=None,
            platform_fee=None,
            payment_fee=None,
            fulfillment_fee=None,
            packaging_cost=None,
            outbound_logistics=shipping,
            return_reverse_logistics=None,
            ads_cost=ads.get(channel, 0.0) if relation_exists(cur, "mtgt", "ads_costs") else None,
            affiliate_commission=None,
            live_cost=None,
            booking_cost=None,
            kol_koc_creator_cost=None,
            content_trade_agency_cost=None,
            backoffice_cost=backoffice,
            payroll=None,
            direct_overhead=None,
            central_opex=None,
            payout=net,
            orders=int(r["orders"]) if r.get("orders") is not None else None,
            success_orders=None,
            cancelled_orders=None,
            quantity=safe_float(r.get("quantity")),
            metric_sources={
                "net_sales": "mtgt.sales_lines.net_revenue",
                "cogs": "mtgt.sales_lines.quantity * unit_cogs",
                "outbound_logistics": "current management rule: 2% net sales",
                "backoffice_cost": "current management rule: 15% net sales",
                "ads_cost": "mtgt.ads_costs.cost_vnd",
            },
            calculation_notes=[
                "MT/GT giữ logic quản trị hiện tại; công thức marketplace CEO chủ yếu áp dụng Shopee/TikTok.",
                "Các khoản không có nguồn dữ liệu không bị tự động gán 0 để trừ.",
            ],
            explicit_missing=[
                "seller_funded_discount",
                "returns_refunds_revenue",
                "vat",
                "platform_fee",
                "payment_fee",
                "fulfillment_fee",
                "return_reverse_logistics",
                "affiliate_commission",
                "live_cost",
                "booking_cost",
                "kol_koc_creator_cost",
                "content_trade_agency_cost",
                "payroll",
                "direct_overhead",
                "central_opex",
            ],
        ))
    return rows


# ============================================================
# Nhanh.vn - split exact by nhanh.orders.label
# ============================================================

def fetch_nhanh(cur, start_date: dt.date, end_date: dt.date) -> Tuple[List[dict], List[dict]]:
    if not relation_exists(cur, "nhanh", "orders") or not relation_exists(cur, "nhanh", "order_items"):
        return [], []

    ads_exists = relation_exists(cur, "nhanh", "ads_costs")
    ads_cols = get_columns(cur, "nhanh", "ads_costs") if ads_exists else set()
    ads_cost_col = "cost_vnd_vat" if "cost_vnd_vat" in ads_cols else ("cost_vnd" if "cost_vnd" in ads_cols else None)

    ads_cte = """
        SELECT NULL::text AS shop_label, NULL::numeric AS ads_cost WHERE false
    """
    if ads_exists and ads_cost_col:
        ads_cte = f"""
            SELECT COALESCE(NULLIF(TRIM(shop_label),''),'DEFAULT')::text AS shop_label,
                   SUM(COALESCE({qident(ads_cost_col)},0))::numeric AS ads_cost
            FROM nhanh.ads_costs
            WHERE ads_date BETWEEN %s AND %s
            GROUP BY COALESCE(NULLIF(TRIM(shop_label),''),'DEFAULT')
        """

    params = [start_date, end_date]
    if ads_exists and ads_cost_col:
        params += [start_date, end_date]

    cur.execute(
        f"""
        WITH item AS (
            SELECT
                i.order_id,
                SUM(COALESCE(i.quantity,0) * COALESCE(i.price,0))::numeric AS gross_sales,
                SUM(CASE WHEN COALESCE(i.is_gift,false) THEN 0
                         ELSE COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) END)::numeric AS sold_cogs,
                SUM(CASE WHEN COALESCE(i.is_gift,false)
                         THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS gift_cogs,
                SUM(COALESCE(i.quantity,0))::numeric AS quantity
            FROM nhanh.order_items i
            GROUP BY i.order_id
        ),
        base AS (
            SELECT
                COALESCE(NULLIF(TRIM(o.label),''),'UNLABELED')::text AS subchannel,
                SUM(COALESCE(i.gross_sales,0))::numeric AS gross_sales,
                SUM(COALESCE(o.order_value,0))::numeric AS net_sales,
                SUM(COALESCE(i.sold_cogs,0))::numeric AS sold_cogs,
                SUM(COALESCE(i.gift_cogs,0))::numeric AS gift_cogs,
                SUM(COALESCE(i.quantity,0))::numeric AS quantity,
                SUM(COALESCE(o.shipping_fee,0))::numeric AS shipping_return_fee,
                SUM(COALESCE(i.quantity,0) * 2000)::numeric AS packaging_cost,
                SUM(
                    CASE
                        WHEN TRIM(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Đã hoàn','Khách hủy')
                        THEN 0
                        ELSE GREATEST(COALESCE(o.order_value,0),0) * 0.15
                    END
                )::numeric AS backoffice_cost,
                COUNT(*)::bigint AS orders,
                COUNT(*) FILTER (
                    WHERE TRIM(COALESCE(o.status,'')) NOT IN ('Đã hủy','Hệ Thống hủy','Đã hoàn','Khách hủy')
                )::bigint AS success_orders,
                COUNT(*) FILTER (
                    WHERE TRIM(COALESCE(o.status,'')) IN ('Đã hủy','Hệ Thống hủy','Đã hoàn','Khách hủy')
                )::bigint AS cancelled_orders
            FROM nhanh.orders o
            LEFT JOIN item i ON i.order_id=o.id
            WHERE o.order_date BETWEEN %s AND %s
            GROUP BY COALESCE(NULLIF(TRIM(o.label),''),'UNLABELED')
        ),
        ads AS (
            {ads_cte}
        )
        SELECT
            b.*,
            a.ads_cost
        FROM base b
        LEFT JOIN ads a ON a.shop_label=b.subchannel
        ORDER BY b.subchannel
        """,
        params,
    )
    raw = rows_to_dicts(cur.fetchall())

    # Ads records that cannot be matched exactly to an order label are kept separate.
    unallocated_ads: List[dict] = []
    if ads_exists and ads_cost_col:
        cur.execute(
            f"""
            WITH labels AS (
                SELECT DISTINCT COALESCE(NULLIF(TRIM(label),''),'UNLABELED')::text AS label
                FROM nhanh.orders
                WHERE order_date BETWEEN %s AND %s
            )
            SELECT
                COALESCE(NULLIF(TRIM(a.shop_label),''),'DEFAULT')::text AS shop_label,
                SUM(COALESCE(a.{qident(ads_cost_col)},0))::numeric AS ads_cost
            FROM nhanh.ads_costs a
            LEFT JOIN labels l
              ON l.label=COALESCE(NULLIF(TRIM(a.shop_label),''),'DEFAULT')
            WHERE a.ads_date BETWEEN %s AND %s
              AND l.label IS NULL
            GROUP BY COALESCE(NULLIF(TRIM(a.shop_label),''),'DEFAULT')
            ORDER BY 1
            """,
            (start_date, end_date, start_date, end_date),
        )
        unallocated_ads = rows_to_dicts(cur.fetchall())

    rows: List[dict] = []
    for r in raw:
        sub = str(r["subchannel"])
        sold = safe_float(r.get("sold_cogs"))
        gift = safe_float(r.get("gift_cogs"))
        cogs_total = None if sold is None or gift is None else sold + gift
        matched_ads = safe_float(r.get("ads_cost"))
        ads_metric = matched_ads if matched_ads is not None else (0.0 if ads_exists and ads_cost_col else None)

        rows.append(row_with_waterfall(
            channel=f"NHANH::{sub}",
            subchannel=sub,
            source_system="NHANH",
            period_start=start_date,
            period_end=end_date,
            gross_sales=safe_float(r.get("gross_sales")),
            seller_funded_discount=None,
            platform_funded_discount=None,
            returns_refunds_revenue=None,
            vat=None,
            net_sales=safe_float(r.get("net_sales")),
            cogs_total=cogs_total,
            sold_cogs=sold,
            gift_promo_cogs=gift,
            platform_fee=None,
            payment_fee=None,
            fulfillment_fee=None,
            packaging_cost=safe_float(r.get("packaging_cost")),
            outbound_logistics=None,
            return_reverse_logistics=safe_float(r.get("shipping_return_fee")),
            ads_cost=ads_metric,
            affiliate_commission=None,
            live_cost=None,
            booking_cost=None,
            kol_koc_creator_cost=None,
            content_trade_agency_cost=None,
            backoffice_cost=safe_float(r.get("backoffice_cost")),
            payroll=None,
            direct_overhead=None,
            central_opex=None,
            payout=safe_float(r.get("net_sales")),
            orders=int(r["orders"]),
            success_orders=int(r["success_orders"]),
            cancelled_orders=int(r["cancelled_orders"]),
            quantity=safe_float(r.get("quantity")),
            metric_sources={
                "subchannel": "nhanh.orders.label exact value",
                "gross_sales": "nhanh.order_items.quantity * price",
                "net_sales": "nhanh.orders.order_value",
                "sold_cogs": "non-gift nhanh.order_items.quantity * unit_cogs",
                "gift_promo_cogs": "gift nhanh.order_items.quantity * unit_cogs",
                "return_reverse_logistics": "nhanh.orders.shipping_fee from v2.12 rule",
                "packaging_cost": "2,000 VND * quantity current report rule",
                "ads_cost": f"nhanh.ads_costs.{ads_cost_col} exact shop_label match" if ads_cost_col else "not_available",
                "backoffice_cost": "15% positive order_value except exact no-BO statuses",
            },
            calculation_notes=[
                "Nhanh được tách theo đúng giá trị nhanh.orders.label; không gộp thành DIGITAL.",
                "Ads chỉ gắn cho kênh nhỏ khi shop_label khớp chính xác label; phần không khớp nằm ở nhanh_unallocated_ads.",
                "Net Sales dùng trực tiếp Giá trị đơn hàng hiện ETL đang lưu, không tự suy ra discount/voucher.",
            ],
            explicit_missing=[
                "seller_funded_discount",
                "returns_refunds_revenue",
                "vat",
                "platform_fee",
                "payment_fee",
                "fulfillment_fee",
                "outbound_logistics",
                "affiliate_commission",
                "live_cost",
                "booking_cost",
                "kol_koc_creator_cost",
                "content_trade_agency_cost",
                "payroll",
                "direct_overhead",
                "central_opex",
            ],
        ))
    return rows, unallocated_ads


# ============================================================
# Shopee CEO formula
# ============================================================

def fetch_shopee(cur, start_date: dt.date, end_date: dt.date, warnings: List[str]) -> List[dict]:
    if not relation_exists(cur, "shopee", "orders"):
        return []

    orders_cols = get_columns(cur, "shopee", "orders")
    extra_exists = relation_exists(cur, "shopee", "order_financials_extra")
    extra_cols = get_columns(cur, "shopee", "order_financials_extra") if extra_exists else set()

    has_new_discount = {
        "seller_subsidy_amount",
        "platform_subsidy_amount",
        "total_subsidy_amount",
        "shop_voucher_amount",
    }.issubset(extra_cols)

    if not has_new_discount:
        warnings.append(
            "Shopee chưa có đủ 4 cột CEO discount bridge trong order_financials_extra; "
            "Gross/Net bridge mới sẽ chỉ dùng dữ liệu nào đang có."
        )

    seller_col = "seller_subsidy_amount" if "seller_subsidy_amount" in extra_cols else (
        "seller_discount" if "seller_discount" in extra_cols else None
    )
    platform_col = "platform_subsidy_amount" if "platform_subsidy_amount" in extra_cols else None
    total_col = "total_subsidy_amount" if "total_subsidy_amount" in extra_cols else None
    voucher_col = "shop_voucher_amount" if "shop_voucher_amount" in extra_cols else None
    gross_col = "gross_sales" if "gross_sales" in extra_cols else None

    # Old platform_voucher held Shop voucher, but old SUM logic can overcount multi-SKU orders.
    # Do NOT silently use it as a fallback for the new CEO bridge.
    if voucher_col is None and "platform_voucher" in extra_cols:
        warnings.append(
            "Shopee có platform_voucher legacy nhưng không dùng làm Shop voucher fallback "
            "vì logic cũ có thể cộng lặp trên đơn nhiều SKU."
        )

    item_exists = relation_exists(cur, "shopee", "order_items")
    ads_exists = relation_exists(cur, "shopee", "ads_costs")
    ads_cols = get_columns(cur, "shopee", "ads_costs") if ads_exists else set()
    ads_col = "cost_vnd_vat" if "cost_vnd_vat" in ads_cols else ("cost_vnd" if "cost_vnd" in ads_cols else None)

    seller_expr = f"COALESCE(f.{qident(seller_col)},0)" if seller_col else "NULL::numeric"
    platform_expr = f"COALESCE(f.{qident(platform_col)},0)" if platform_col else "NULL::numeric"
    total_expr = f"COALESCE(f.{qident(total_col)},0)" if total_col else "NULL::numeric"
    voucher_expr = f"COALESCE(f.{qident(voucher_col)},0)" if voucher_col else "NULL::numeric"
    gross_expr = (
        f"COALESCE(f.{qident(gross_col)},o.gross_sales,0)"
        if gross_col and "gross_sales" in orders_cols
        else (f"COALESCE(f.{qident(gross_col)},0)" if gross_col else "COALESCE(o.gross_sales,0)")
    )
    extra_join = """
        LEFT JOIN shopee.order_financials_extra f
          ON f.external_order_id=o.external_order_id
         AND f.shop_label=o.shop_label
    """ if extra_exists else "LEFT JOIN (SELECT NULL::text external_order_id, NULL::text shop_label) f ON false"

    item_cte = """
        SELECT
            oi.order_id,
            SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0
                     WHEN COALESCE(oi.is_gift,false) THEN 0
                     ELSE COALESCE(oi.quantity,0)*COALESCE(oi.unit_cogs,0) END)::numeric AS sold_cogs,
            SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0
                     WHEN COALESCE(oi.is_gift,false)
                     THEN COALESCE(oi.quantity,0)*COALESCE(oi.unit_cogs,0) ELSE 0 END)::numeric AS gift_cogs,
            SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0
                     ELSE COALESCE(oi.quantity,0) END)::numeric AS quantity
        FROM shopee.order_items oi
        JOIN shopee.orders o ON o.id=oi.order_id
        GROUP BY oi.order_id
    """ if item_exists else """
        SELECT NULL::bigint AS order_id, NULL::numeric sold_cogs,
               NULL::numeric gift_cogs, NULL::numeric quantity WHERE false
    """

    ads_sql = "NULL::numeric"
    if ads_exists and ads_col:
        ads_sql = f"""
            (SELECT SUM(COALESCE(a.{qident(ads_col)},0))::numeric
             FROM shopee.ads_costs a
             WHERE a.ads_date BETWEEN %s AND %s)
        """

    params: List[Any] = [start_date, end_date]
    if ads_exists and ads_col:
        params += [start_date, end_date]

    cur.execute(
        f"""
        WITH item AS (
            {item_cte}
        ),
        order_base AS (
            SELECT
                o.id,
                o.external_order_id,
                o.shop_label,
                o.order_date,
                COALESCE(o.is_cancelled,false) AS is_cancelled,
                ({gross_expr})::numeric AS gross_sales,
                ({seller_expr})::numeric AS seller_subsidy,
                ({platform_expr})::numeric AS platform_subsidy,
                ({total_expr})::numeric AS total_subsidy,
                ({voucher_expr})::numeric AS shop_voucher,
                COALESCE(i.sold_cogs,0)::numeric AS sold_cogs,
                COALESCE(i.gift_cogs,0)::numeric AS gift_cogs,
                COALESCE(i.quantity,0)::numeric AS quantity,
                COALESCE(o.fixed_fee,0)::numeric AS fixed_fee,
                COALESCE(o.service_fee,0)::numeric AS service_fee,
                COALESCE(o.payment_fee,0)::numeric AS payment_fee,
                COALESCE(o.affiliate_amount,0)::numeric AS affiliate_amount,
                COALESCE(o.return_fee,0)::numeric AS return_fee,
                CASE
                    WHEN COALESCE(o.payout_actual,0) <> 0 THEN COALESCE(o.payout_actual,0)
                    ELSE COALESCE(o.est_payout,0)
                END::numeric AS payout
            FROM shopee.orders o
            {extra_join}
            LEFT JOIN item i ON i.order_id=o.id
            WHERE o.order_date BETWEEN %s AND %s
        )
        SELECT
            SUM(CASE WHEN is_cancelled THEN 0 ELSE gross_sales END)::numeric AS gross_sales,
            SUM(CASE WHEN is_cancelled THEN 0 ELSE seller_subsidy END)::numeric AS seller_subsidy,
            SUM(CASE WHEN is_cancelled THEN 0 ELSE platform_subsidy END)::numeric AS platform_subsidy,
            SUM(CASE WHEN is_cancelled THEN 0 ELSE total_subsidy END)::numeric AS total_subsidy,
            SUM(CASE WHEN is_cancelled THEN 0 ELSE shop_voucher END)::numeric AS shop_voucher,
            SUM(
                CASE
                    WHEN is_cancelled THEN 0
                    WHEN seller_subsidy IS NULL OR shop_voucher IS NULL THEN NULL
                    ELSE gross_sales - seller_subsidy - shop_voucher
                END
            )::numeric AS net_sales_formula,
            SUM(sold_cogs)::numeric AS sold_cogs,
            SUM(gift_cogs)::numeric AS gift_cogs,
            SUM(quantity)::numeric AS quantity,
            SUM(CASE WHEN is_cancelled THEN 0 ELSE fixed_fee+service_fee END)::numeric AS platform_fee,
            SUM(CASE WHEN is_cancelled THEN 0 ELSE payment_fee END)::numeric AS payment_fee,
            SUM(affiliate_amount)::numeric AS affiliate_commission,
            SUM(return_fee)::numeric AS return_reverse_logistics,
            SUM(quantity*2000)::numeric AS packaging_cost,
            SUM(payout)::numeric AS payout,
            COUNT(*)::bigint AS orders,
            COUNT(*) FILTER (WHERE NOT is_cancelled)::bigint AS success_orders,
            COUNT(*) FILTER (WHERE is_cancelled)::bigint AS cancelled_orders,
            {ads_sql} AS ads_cost
        FROM order_base
        """,
        params,
    )
    r = clean_value(dict(cur.fetchone()))

    gross = safe_float(r.get("gross_sales"))
    seller = safe_float(r.get("seller_subsidy")) if seller_col else None
    platform_sub = safe_float(r.get("platform_subsidy")) if platform_col else None
    voucher = safe_float(r.get("shop_voucher")) if voucher_col else None
    net = safe_float(r.get("net_sales_formula")) if seller_col and voucher_col else None
    sold = safe_float(r.get("sold_cogs")) if item_exists else None
    gift = safe_float(r.get("gift_cogs")) if item_exists else None
    cogs_total = None if sold is None or gift is None else sold + gift
    payout = safe_float(r.get("payout"))
    backoffice = None if payout is None else max(payout, 0.0) * 0.15

    # Detect likely un-backfilled legacy rows: old data exists but new fields remain default 0.
    if has_new_discount and {"seller_discount", "platform_voucher"}.intersection(extra_cols):
        conditions = []
        if "seller_discount" in extra_cols:
            conditions.append("(COALESCE(seller_discount,0) <> 0 AND COALESCE(seller_subsidy_amount,0)=0)")
        if "platform_voucher" in extra_cols:
            conditions.append("(COALESCE(platform_voucher,0) <> 0 AND COALESCE(shop_voucher_amount,0)=0)")
        if conditions:
            cur.execute(
                f"""
                SELECT COUNT(*)::bigint AS n
                FROM shopee.order_financials_extra
                WHERE order_date BETWEEN %s AND %s
                  AND ({' OR '.join(conditions)})
                """,
                (start_date, end_date),
            )
            n = int(cur.fetchone()["n"])
            if n:
                warnings.append(
                    f"Shopee có {n} order_financials_extra trong kỳ có dấu hiệu chưa backfill/re-run ETL "
                    "cho các cột CEO discount mới; cần re-run dữ liệu kỳ để bridge chính xác."
                )

    explicit_missing = [
        "returns_refunds_revenue",
        "vat",
        "fulfillment_fee",
        "outbound_logistics",
        "live_cost",
        "booking_cost",
        "kol_koc_creator_cost",
        "content_trade_agency_cost",
        "payroll",
        "direct_overhead",
        "central_opex",
    ]
    if seller_col is None:
        explicit_missing.append("seller_funded_discount")
    if voucher_col is None:
        explicit_missing.append("shop_voucher")
    if platform_col is None:
        explicit_missing.append("platform_funded_discount")

    return [row_with_waterfall(
        channel="SHOPEE",
        subchannel=None,
        source_system="SHOPEE",
        period_start=start_date,
        period_end=end_date,
        gross_sales=gross,
        seller_funded_discount=(None if seller is None and voucher is None else nz(seller)+nz(voucher)),
        platform_funded_discount=platform_sub,
        returns_refunds_revenue=None,
        vat=None,
        net_sales=net,
        cogs_total=cogs_total,
        sold_cogs=sold,
        gift_promo_cogs=gift,
        platform_fee=safe_float(r.get("platform_fee")),
        payment_fee=safe_float(r.get("payment_fee")),
        fulfillment_fee=None,
        packaging_cost=safe_float(r.get("packaging_cost")),
        outbound_logistics=None,
        return_reverse_logistics=safe_float(r.get("return_reverse_logistics")),
        ads_cost=safe_float(r.get("ads_cost")) if ads_exists and ads_col else None,
        affiliate_commission=safe_float(r.get("affiliate_commission")),
        live_cost=None,
        booking_cost=None,
        kol_koc_creator_cost=None,
        content_trade_agency_cost=None,
        backoffice_cost=backoffice,
        payroll=None,
        direct_overhead=None,
        central_opex=None,
        payout=payout,
        orders=int(r["orders"]) if r.get("orders") is not None else None,
        success_orders=int(r["success_orders"]) if r.get("success_orders") is not None else None,
        cancelled_orders=int(r["cancelled_orders"]) if r.get("cancelled_orders") is not None else None,
        quantity=safe_float(r.get("quantity")),
        metric_sources={
            "gross_sales": "shopee.order_financials_extra.gross_sales / shopee.orders.gross_sales",
            "seller_subsidy": f"shopee.order_financials_extra.{seller_col}" if seller_col else "not_available",
            "shop_voucher": f"shopee.order_financials_extra.{voucher_col}" if voucher_col else "not_available",
            "platform_subsidy": f"shopee.order_financials_extra.{platform_col}" if platform_col else "not_available",
            "net_sales": "CEO formula: gross - seller subsidy - Shop voucher; cancelled order = 0",
            "cogs": "shopee.order_items quantity * unit_cogs, split by is_gift",
            "platform_fee": "shopee.orders.fixed_fee + service_fee",
            "payment_fee": "shopee.orders.payment_fee",
            "return_reverse_logistics": "shopee.orders.return_fee",
            "packaging_cost": "current report rule: 2,000 VND * successful item quantity",
            "ads_cost": f"shopee.ads_costs.{ads_col}" if ads_col else "not_available",
            "affiliate_commission": "shopee.orders.affiliate_amount; CM2, not platform fee",
            "backoffice_cost": "current management rule: 15% positive aggregate payout",
            "payout": "payout_actual if non-zero else est_payout",
        },
        calculation_notes=[
            "Không trừ trợ giá do Shopee tài trợ khỏi Net Sales.",
            "Affiliate được đưa xuống CM2, không gộp vào CM1/platform fee.",
            "Refund doanh thu chưa có field chuẩn trong DB hiện tại nên không tự ước tính và không trừ.",
            "EBITDA available chỉ trừ chi phí hiện đang có; payroll/direct overhead/central OPEX chưa có thì không tự thêm.",
        ],
        explicit_missing=explicit_missing,
    )]


# ============================================================
# TikTok CEO formula
# ============================================================

def fetch_tiktok(cur, start_date: dt.date, end_date: dt.date, warnings: List[str]) -> List[dict]:
    if not relation_exists(cur, "tiktok", "orders_pnl"):
        return []

    cols = get_columns(cur, "tiktok", "orders_pnl")
    seller_discount_col = "seller_discount_amount" if "seller_discount_amount" in cols else None
    if seller_discount_col is None:
        warnings.append(
            "TikTok chưa có tiktok.orders_pnl.seller_discount_amount; "
            "Net Sales vẫn lấy fee_base_revenue nhưng Gross Sales CEO bridge không thể dựng đầy đủ."
        )

    ads_exists = relation_exists(cur, "tiktok", "ads_costs")
    live_exists = relation_exists(cur, "tiktok", "live_costs")
    ads_cols = get_columns(cur, "tiktok", "ads_costs") if ads_exists else set()
    ads_col = "cost_vnd_vat" if "cost_vnd_vat" in ads_cols else ("cost_vnd" if "cost_vnd" in ads_cols else None)

    seller_expr = f"COALESCE(o.{qident(seller_discount_col)},0)" if seller_discount_col else "NULL::numeric"
    gross_expr = (
        f"CASE WHEN COALESCE(o.is_cancelled,false) THEN 0 "
        f"ELSE COALESCE(o.fee_base_revenue,0)+COALESCE(o.{qident(seller_discount_col)},0) END"
        if seller_discount_col
        else "NULL::numeric"
    )

    cur.execute(
        f"""
        SELECT
            SUM(({gross_expr})::numeric)::numeric AS gross_sales,
            SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0 ELSE ({seller_expr})::numeric END)::numeric AS seller_discount,
            SUM(CASE WHEN COALESCE(o.is_cancelled,false) THEN 0 ELSE COALESCE(o.fee_base_revenue,0) END)::numeric AS net_sales,
            SUM(COALESCE(o.cogs_amount,0))::numeric AS cogs_total,
            SUM(COALESCE(o.total_sku_qty,0))::numeric AS quantity,
            SUM(COALESCE(o.fixed_fee,0)+COALESCE(o.vxp_fee,0)+COALESCE(o.infrastructure_fee,0))::numeric AS platform_fee,
            SUM(COALESCE(o.payment_fee,0))::numeric AS payment_fee,
            SUM(COALESCE(o.return_shipping_fee,0))::numeric AS return_reverse_logistics,
            SUM(COALESCE(o.packaging_cost,0))::numeric AS packaging_cost,
            SUM(COALESCE(o.commission_amount,0))::numeric AS affiliate_commission,
            SUM(COALESCE(o.booking_fee,0))::numeric AS booking_cost,
            SUM(COALESCE(o.backoffice_cost,0))::numeric AS backoffice_cost,
            SUM(
                CASE
                    WHEN COALESCE(o.settlement_paid,0)+COALESCE(o.settlement_unpaid,0) <> 0
                    THEN COALESCE(o.settlement_paid,0)+COALESCE(o.settlement_unpaid,0)
                    ELSE COALESCE(o.estimated_payout,0)
                END
            )::numeric AS payout,
            COUNT(*)::bigint AS orders,
            COUNT(*) FILTER (WHERE NOT COALESCE(o.is_cancelled,false))::bigint AS success_orders,
            COUNT(*) FILTER (WHERE COALESCE(o.is_cancelled,false))::bigint AS cancelled_orders
        FROM tiktok.orders_pnl o
        WHERE o.order_date BETWEEN %s AND %s
        """,
        (start_date, end_date),
    )
    r = clean_value(dict(cur.fetchone()))

    ads_cost: Optional[float] = None
    if ads_exists and ads_col:
        cur.execute(
            f"""
            SELECT SUM(COALESCE({qident(ads_col)},0))::numeric AS cost
            FROM tiktok.ads_costs
            WHERE ads_date BETWEEN %s AND %s
            """,
            (start_date, end_date),
        )
        ads_cost = nz(cur.fetchone()["cost"])

    teaser_cost: Optional[float] = None
    in_house_cost: Optional[float] = None
    if live_exists:
        live_cols = get_columns(cur, "tiktok", "live_costs")
        if "teaser_cost" in live_cols or "in_house_cost" in live_cols:
            cur.execute(
                """
                SELECT
                    SUM(COALESCE(teaser_cost,0))::numeric AS teaser_cost,
                    SUM(COALESCE(in_house_cost,0))::numeric AS in_house_cost
                FROM tiktok.live_costs
                WHERE live_date BETWEEN %s AND %s
                """,
                (start_date, end_date),
            )
            lr = cur.fetchone()
            teaser_cost = nz(lr["teaser_cost"]) if "teaser_cost" in live_cols else None
            in_house_cost = nz(lr["in_house_cost"]) if "in_house_cost" in live_cols else None

    # Current reporting treats teaser as advertising.
    if teaser_cost is not None:
        ads_cost = (0.0 if ads_cost is None else ads_cost) + teaser_cost

    seller = safe_float(r.get("seller_discount")) if seller_discount_col else None
    net = safe_float(r.get("net_sales"))
    gross = safe_float(r.get("gross_sales")) if seller_discount_col else None

    explicit_missing = [
        "platform_funded_discount",
        "returns_refunds_revenue",
        "vat",
        "gift_promo_cogs_separate",
        "fulfillment_fee",
        "outbound_logistics",
        "kol_koc_creator_cost_separate",
        "content_trade_agency_cost",
        "payroll",
        "direct_overhead",
        "central_opex",
    ]
    if seller_discount_col is None:
        explicit_missing.append("seller_funded_discount")

    return [row_with_waterfall(
        channel="TIKTOK",
        subchannel=None,
        source_system="TIKTOK",
        period_start=start_date,
        period_end=end_date,
        gross_sales=gross,
        seller_funded_discount=seller,
        platform_funded_discount=None,
        returns_refunds_revenue=None,
        vat=None,
        net_sales=net,
        cogs_total=safe_float(r.get("cogs_total")),
        sold_cogs=None,
        gift_promo_cogs=None,
        platform_fee=safe_float(r.get("platform_fee")),
        payment_fee=safe_float(r.get("payment_fee")),
        fulfillment_fee=None,
        packaging_cost=safe_float(r.get("packaging_cost")),
        outbound_logistics=None,
        return_reverse_logistics=safe_float(r.get("return_reverse_logistics")),
        ads_cost=ads_cost,
        affiliate_commission=safe_float(r.get("affiliate_commission")),
        live_cost=in_house_cost,
        booking_cost=safe_float(r.get("booking_cost")),
        kol_koc_creator_cost=None,
        content_trade_agency_cost=None,
        backoffice_cost=safe_float(r.get("backoffice_cost")),
        payroll=None,
        direct_overhead=None,
        central_opex=None,
        payout=safe_float(r.get("payout")),
        orders=int(r["orders"]) if r.get("orders") is not None else None,
        success_orders=int(r["success_orders"]) if r.get("success_orders") is not None else None,
        cancelled_orders=int(r["cancelled_orders"]) if r.get("cancelled_orders") is not None else None,
        quantity=safe_float(r.get("quantity")),
        metric_sources={
            "gross_sales": "fee_base_revenue + seller_discount_amount for non-cancelled orders",
            "seller_funded_discount": f"tiktok.orders_pnl.{seller_discount_col}" if seller_discount_col else "not_available",
            "net_sales": "tiktok.orders_pnl.fee_base_revenue; cancelled order = 0",
            "cogs_total": "tiktok.orders_pnl.cogs_amount (gift split not separately stored)",
            "platform_fee": "fixed_fee + vxp_fee + infrastructure_fee",
            "payment_fee": "payment_fee",
            "return_reverse_logistics": "return_shipping_fee",
            "packaging_cost": "packaging_cost",
            "ads_cost": f"tiktok.ads_costs.{ads_col} + live_costs.teaser_cost" if ads_col else "live_costs.teaser_cost if available",
            "affiliate_commission": "tiktok.orders_pnl.commission_amount",
            "live_cost": "tiktok.live_costs.in_house_cost",
            "booking_cost": "tiktok.orders_pnl.booking_fee",
            "backoffice_cost": "tiktok.orders_pnl.backoffice_cost",
            "payout": "settlement_paid + settlement_unpaid, fallback estimated_payout",
        },
        calculation_notes=[
            "SKU Seller Discount là khoản người bán tài trợ và được trừ khỏi Gross Sales.",
            "Không tự trừ Platform Discount vì chưa được lưu thành field riêng trong DB hiện tại.",
            "commission_amount được xếp CM2 theo yêu cầu CEO.",
            "COGS TikTok hiện dùng cogs_amount tổng; chưa có split gift riêng nên không double-count gift.",
            "Refund revenue chưa có field chuẩn trong DB hiện tại nên không tự ước tính và không trừ.",
        ],
        explicit_missing=explicit_missing,
    )]


# ============================================================
# Summary / extras
# ============================================================

def summarize_leaf_rows(rows: List[dict], period_start: dt.date, period_end: dt.date) -> dict:
    def sum_metric(name: str) -> Optional[float]:
        vals = [safe_float(r.get(name)) for r in rows if r.get(name) is not None]
        return None if not vals else sum(v for v in vals if v is not None)

    partial_channels = [
        r["channel"] for r in rows
        if r.get("calculation_status") == "AVAILABLE_PARTIAL"
    ]
    return clean_value({
        "period_start": period_start,
        "period_end": period_end,
        "leaf_channels": [r["channel"] for r in rows],
        "gross_sales": sum_metric("gross_sales"),
        "net_sales": sum_metric("net_sales"),
        "gm1": sum_metric("gm1"),
        "cm1": sum_metric("cm1"),
        "cm2": sum_metric("cm2"),
        "ebitda_available": sum_metric("ebitda_available"),
        "orders": sum(int(r.get("orders") or 0) for r in rows),
        "success_orders": sum(int(r.get("success_orders") or 0) for r in rows),
        "cancelled_orders": sum(int(r.get("cancelled_orders") or 0) for r in rows),
        "partial_channels": partial_channels,
        "calculation_status": "AVAILABLE_PARTIAL" if partial_channels else "AVAILABLE_FULL",
        "note": (
            "TOTAL chỉ cộng các leaf rows MT, GT, từng NHANH::label, SHOPEE, TIKTOK. "
            "Không cộng lại NHANH_TOTAL/ECOM để tránh double count."
        ),
    })


def build_group_summaries(rows: List[dict]) -> dict:
    nhanh_rows = [r for r in rows if str(r.get("channel","")).startswith("NHANH::")]
    ecom_rows = [r for r in rows if r.get("channel") in {"SHOPEE","TIKTOK"}]

    def group(name: str, items: List[dict]) -> dict:
        def sm(metric: str):
            vals = [safe_float(r.get(metric)) for r in items if r.get(metric) is not None]
            return None if not vals else sum(v for v in vals if v is not None)
        return clean_value({
            "group": name,
            "members": [r["channel"] for r in items],
            "net_sales": sm("net_sales"),
            "gm1": sm("gm1"),
            "cm1": sm("cm1"),
            "cm2": sm("cm2"),
            "ebitda_available": sm("ebitda_available"),
            "is_reference_aggregate": True,
            "exclude_from_total_reaggregation": True,
        })

    return {
        "NHANH_TOTAL_REFERENCE": group("NHANH_TOTAL_REFERENCE", nhanh_rows),
        "ECOM_REFERENCE": group("ECOM_REFERENCE", ecom_rows),
    }


def fetch_mtd_top_sku(cur, report_date: dt.date, warnings: List[str]) -> List[dict]:
    if not relation_exists(cur, "mart", "v_growth_top_sku_monthly_sales_level"):
        warnings.append("Missing mart.v_growth_top_sku_monthly_sales_level; skip MTD top SKU.")
        return []
    cur.execute(
        """
        SELECT *
        FROM mart.v_growth_top_sku_monthly_sales_level
        WHERE as_of_date=%s
        ORDER BY rank_by_qty, rank_by_net_sales
        LIMIT 50
        """,
        (report_date,),
    )
    return rows_to_dicts(cur.fetchall())


def fetch_mtgt_top_customers(cur, report_date: dt.date, warnings: List[str]) -> List[dict]:
    view = "v_growth_mtgt_top5_customers_monthly_by_channel"
    if not relation_exists(cur, "mart", view):
        warnings.append(f"Missing mart.{view}; skip MT/GT top customers.")
        return []
    cur.execute(
        f"""
        SELECT MAX(as_of_date)::date AS d
        FROM mart.{view}
        WHERE as_of_date <= %s
        """,
        (report_date,),
    )
    d = cur.fetchone()["d"]
    if not d:
        return []
    cur.execute(
        f"""
        SELECT *
        FROM mart.{view}
        WHERE as_of_date=%s
        ORDER BY channel_group, rank_channel_by_net_sales
        """,
        (d,),
    )
    return rows_to_dicts(cur.fetchall())


# ============================================================
# Package builder
# ============================================================

def build_period(cur, start_date: dt.date, end_date: dt.date, warnings: List[str]) -> Tuple[List[dict], List[dict]]:
    mtgt = fetch_mtgt(cur, start_date, end_date)
    nhanh, nhanh_unallocated_ads = fetch_nhanh(cur, start_date, end_date)
    shopee = fetch_shopee(cur, start_date, end_date, warnings)
    tiktok = fetch_tiktok(cur, start_date, end_date, warnings)

    # Leaf-only list. No DIGITAL, NHANH parent, or ECOM parent.
    rows = mtgt + nhanh + shopee + tiktok
    return rows, nhanh_unallocated_ads


def build_package(report_date_str: str) -> Dict[str, Any]:
    report_date = parse_date(report_date_str)
    mtd_start = month_start(report_date)
    warnings: List[str] = []
    errors: List[str] = []
    notes: List[str] = []

    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            daily_rows, daily_nhanh_unallocated_ads = build_period(
                cur, report_date, report_date, warnings
            )
            mtd_rows, mtd_nhanh_unallocated_ads = build_period(
                cur, mtd_start, report_date, warnings
            )

            mtd_top_sku = fetch_mtd_top_sku(cur, report_date, warnings)
            mtd_top_mtgt_customers = fetch_mtgt_top_customers(cur, report_date, warnings)

            # Coverage checks for mandatory channel families.
            present_daily = {r["channel"] for r in daily_rows}
            present_mtd = {r["channel"] for r in mtd_rows}
            for channel in ("MT", "GT", "SHOPEE", "TIKTOK"):
                if channel not in present_mtd:
                    warnings.append(f"MTD không có leaf row {channel}.")
            if not any(str(x).startswith("NHANH::") for x in present_mtd):
                warnings.append("MTD không có kênh nhỏ NHANH::label.")

    daily_summary = summarize_leaf_rows(daily_rows, report_date, report_date)
    mtd_summary = summarize_leaf_rows(mtd_rows, mtd_start, report_date)

    package = {
        "metadata": {
            "report_type": PACKAGE_TYPE,
            "report_title": "CEO Daily Cumulative Report",
            "package_version": PACKAGE_VERSION,
            "package_version_int": PACKAGE_VERSION_INT,
            "report_date": report_date.isoformat(),
            "daily_period_start": report_date.isoformat(),
            "daily_period_end": report_date.isoformat(),
            "mtd_period_start": mtd_start.isoformat(),
            "mtd_period_end": report_date.isoformat(),
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "scope": (
                "Báo cáo chi tiết ngày + lũy kế tháng. "
                "Không có khối tuần/tháng. Shopee/TikTok theo CEO waterfall từ dữ liệu thực có."
            ),
        },
        "data_quality": {
            "status": "FAIL" if errors else ("WARNING" if warnings else "PASS"),
            "can_analyze": len(errors) == 0,
            "errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "notes": sorted(set(notes)),
            "missing_metric_policy": (
                "Không có nguồn dữ liệu => value=None, ghi excluded_missing_metrics, "
                "không ép 0 và không trừ vào stage. Stage đó được đánh dấu AVAILABLE_PARTIAL."
            ),
            "nhanh_subchannel_policy": (
                "Tách đúng theo nhanh.orders.label; ads chỉ ghép khi shop_label khớp exact label."
            ),
            "total_policy": (
                "TOTAL chỉ cộng leaf rows: MT, GT, NHANH::<label>, SHOPEE, TIKTOK."
            ),
        },
        "metric_definitions": {
            "net_sales_ceo": (
                "Gross Sales - seller-funded discount/voucher - return/refund available - VAT available. "
                "Khoản platform-funded không trừ."
            ),
            "gm1": "Net Sales - COGS hàng bán - COGS quà tặng/khuyến mại; nếu chưa split thì trừ cogs_total một lần.",
            "cm1": (
                "GM1 - platform fee - payment fee - fulfillment - packaging - outbound logistics "
                "- return/reverse logistics, chỉ trừ metric có dữ liệu."
            ),
            "cm2": (
                "CM1 - ads - affiliate - live - booking - KOL/KOC/creator - content/trade/agency, "
                "chỉ trừ metric có dữ liệu."
            ),
            "ebitda_available": (
                "CM2 - backoffice/payroll/direct overhead/central OPEX có dữ liệu. "
                "Không đồng nghĩa EBITDA kế toán hoàn chỉnh khi còn excluded_missing_metrics."
            ),
        },
        "daily_summary": daily_summary,
        "daily_by_leaf_channel": daily_rows,
        "daily_group_reference": build_group_summaries(daily_rows),
        "daily_nhanh_unallocated_ads": daily_nhanh_unallocated_ads,
        "mtd_summary": mtd_summary,
        "mtd_by_leaf_channel": mtd_rows,
        "mtd_group_reference": build_group_summaries(mtd_rows),
        "mtd_nhanh_unallocated_ads": mtd_nhanh_unallocated_ads,
        "mtd_top_sku_sales_level": mtd_top_sku,
        "mtd_top_mtgt_customers_by_channel": mtd_top_mtgt_customers,
        "analysis_instructions": {
            "primary_blocks": [
                "daily_by_leaf_channel",
                "mtd_by_leaf_channel",
            ],
            "required_waterfall": "Gross Sales -> Net Sales -> GM1 -> CM1 -> CM2 -> EBITDA available",
            "rules": [
                "Không tự tính lại số nguồn nếu package đã có stage.",
                "Không đổi None/missing thành 0.",
                "Không cộng NHANH_TOTAL_REFERENCE hoặc ECOM_REFERENCE vào TOTAL lần nữa.",
                "Nhanh phải phân tích từng NHANH::<label> riêng.",
                "Shopee: không trừ platform-funded subsidy khỏi Net Sales.",
                "Shopee affiliate thuộc CM2, không thuộc platform fee/CM1.",
                "TikTok SKU Seller Discount là seller-funded deduction.",
                "Nếu calculation_status=AVAILABLE_PARTIAL phải nêu rõ các excluded_missing_metrics.",
                "Không gọi ebitda_available là EBITDA kế toán hoàn chỉnh khi còn chi phí bị thiếu.",
                "Ưu tiên so sánh ngày hiện tại với MTD cùng định nghĩa; package này không có weekly/monthly comparison.",
            ],
        },
        "report_rendering_policy": {
            "report_name": "Báo cáo chi tiết CEO ngày & lũy kế tháng",
            "sections": [
                "Tổng quan ngày",
                "Chi tiết từng kênh ngày",
                "Tổng quan lũy kế tháng",
                "Chi tiết từng kênh lũy kế tháng",
                "Chi tiết từng kênh nhỏ Nhanh",
                "Waterfall Shopee/TikTok: Gross -> Net -> GM1 -> CM1 -> CM2 -> EBITDA available",
                "Top SKU lũy kế",
                "Top khách hàng MT/GT",
                "Cảnh báo dữ liệu thiếu/partial",
            ],
            "must_not_merge_nhanh_subchannels": True,
            "must_not_recalculate_package_numbers": True,
            "must_show_partial_status": True,
            "must_not_show_internal_db_view_names": True,
        },
    }
    return clean_value(package)


# ============================================================
# Markdown quick check
# ============================================================

def money(v: Any) -> str:
    if v is None:
        return "N/A"
    return f"{float(v):,.0f}"


def build_md(package: Dict[str, Any]) -> str:
    meta = package["metadata"]
    dq = package["data_quality"]
    ds = package["daily_summary"]
    ms = package["mtd_summary"]

    lines = [
        f"# {meta['report_title']} ({meta['report_date']})",
        "",
        "## Data quality",
        f"- Status: **{dq['status']}**",
        f"- Can analyze: **{dq['can_analyze']}**",
        "",
        "## Daily",
        f"- Net Sales: **{money(ds.get('net_sales'))}**",
        f"- GM1: **{money(ds.get('gm1'))}**",
        f"- CM1: **{money(ds.get('cm1'))}**",
        f"- CM2: **{money(ds.get('cm2'))}**",
        f"- EBITDA available: **{money(ds.get('ebitda_available'))}**",
        "",
        "## MTD",
        f"- Net Sales: **{money(ms.get('net_sales'))}**",
        f"- GM1: **{money(ms.get('gm1'))}**",
        f"- CM1: **{money(ms.get('cm1'))}**",
        f"- CM2: **{money(ms.get('cm2'))}**",
        f"- EBITDA available: **{money(ms.get('ebitda_available'))}**",
        "",
        "## Leaf channels MTD",
    ]
    for r in package["mtd_by_leaf_channel"]:
        lines.append(
            f"- {r['channel']}: Net={money(r.get('net_sales'))}; "
            f"GM1={money(r.get('gm1'))}; CM1={money(r.get('cm1'))}; "
            f"CM2={money(r.get('cm2'))}; EBITDA available={money(r.get('ebitda_available'))}; "
            f"status={r.get('calculation_status')}"
        )
    if dq["warnings"]:
        lines += ["", "## Warnings"] + [f"- {x}" for x in dq["warnings"]]
    lines += [
        "",
        "> File .md chỉ dùng để kiểm nhanh. Gửi file .json cho tác nhân phân tích.",
    ]
    return "\n".join(lines) + "\n"


# ============================================================
# Store DB best-effort (compatible with old builder)
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

            cur.execute(
                """
                SELECT column_name, data_type, udt_name
                FROM information_schema.columns
                WHERE table_schema='control_tower'
                  AND table_name='report_packages'
                """
            )
            colmap = {r["column_name"]: r for r in cur.fetchall()}
            values: Dict[str, Any] = {}

            def add(names: Iterable[str], value: Any) -> None:
                for name in names:
                    if name in colmap and name not in values:
                        values[name] = value
                        return

            add(["report_date", "package_date", "date"], report_date)
            add(["report_type", "package_type", "type"], PACKAGE_TYPE)

            for version_col in ["version", "package_version", "schema_version", "package_schema_version"]:
                if version_col in colmap:
                    dtype = str(colmap[version_col]["data_type"]).lower()
                    udt = str(colmap[version_col]["udt_name"]).lower()
                    values[version_col] = (
                        PACKAGE_VERSION_INT
                        if "int" in dtype or udt in {"int2", "int4", "int8"}
                        else PACKAGE_VERSION
                    )
                    break

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
# CLI
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

    try:
        package = build_package(report_date)
    except Exception as exc:
        print(f"ERROR building CEO daily cumulative package: {exc}", file=sys.stderr)
        raise

    json_path = out_dir / f"ceo_daily_cumulative_package_{report_date}.json"
    md_path = out_dir / f"ceo_daily_cumulative_package_{report_date}.md"

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
    if dq["warnings"]:
        print("Warnings:")
        for w in dq["warnings"]:
            print(f"- {w}")
    if dq["errors"]:
        print("Errors:")
        for e in dq["errors"]:
            print(f"- {e}")

    return 0 if dq["can_analyze"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
