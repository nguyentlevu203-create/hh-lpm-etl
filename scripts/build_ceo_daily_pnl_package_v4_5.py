#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Build CEO Daily Cumulative P&L package v4.5.

This script is designed to be placed in the repository's ``scripts`` folder.

What it does
------------
1. Calls the current week/month builder v3.3, preserving all existing blocks so
   00_Tong_quan, 01_GT and 02_MT can continue to use the current data contract.
2. Adds a daily P&L package for report_date versus a comparison date.
3. Adds detailed MTD-by-day cost rows for Nhanh, Shopee and TikTok.
4. Splits Shopee/TikTok platform fees and affiliate/commission so the Excel
   renderer can show them explicitly and avoid double counting.
5. Calculates the requested waterfall:

   Net Sales = Gross Sales all statuses - exact-cancel sales - seller-funded discounts/vouchers - other adjustments
   GM1       = Net Sales - COGS hàng bán - COGS hàng tặng/khuyến mại
   CM1       = GM1 - platform/payment/fulfilment/packaging/cancel-related logistics costs
   CM2       = CM1 - ads/affiliate/KOL-KOC/live/content/trade/agency direct cost
   Profit    = CM2 - backoffice/direct operating cost

Important
---------
* Costs are stored as positive amounts in JSON. ``pnl_lines`` contains
  ``pnl_effect`` as a negative number for Excel presentation.
* Shopee affiliate is allocated to CM2. Do not subtract the aggregate legacy
  ``platform_fees`` again.
* TikTok creator/partner commission is allocated to CM2. The package keeps a
  reconciliation versus the legacy scorecard profit.
* Backoffice is allocated below CM2 and the final subtotal is Profit.
* Optional ex-VAT revenue can be enabled with ``--vat-rates``. No VAT rate is
  guessed.

Example
-------
python scripts/build_ceo_daily_pnl_package_v4_5.py \
  --date 2026-08-12 \
  --compare-mode previous_day \
  --vat-rates '{"NHANH":0.08,"SHOPEE":0.08,"TIKTOK":0.08}' \
  --out-dir outbox/packages \
  --store-db
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import hashlib
import importlib.util
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:
    import psycopg2
    import psycopg2.extras
except ModuleNotFoundError:
    psycopg2 = None  # type: ignore[assignment]


PACKAGE_TYPE = "CEO_DAILY_CUMULATIVE_PNL"
PACKAGE_VERSION = "v4.5"
PACKAGE_VERSION_INT = 45
BASE_BUILDER = "build_ceo_growth_package_v3_3_week_month.py"
EXPECTED_CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
PRESERVE_SHEETS: Tuple[str, ...] = ("00_Tong_quan", "01_GT", "02_MT")
DAILY_PNL_SHEETS = {
    "NHANH": "03_Nhanh",
    "SHOPEE": "04_Shopee",
    "TIKTOK": "05_TikTok",
}
EPSILON = 1.0
CANCELLED_STATUS_EXACT = "đã hủy"

# Nhanh.vn leaf channels are keyed only from the exact ETL-approved ``label`` values.
# These seven labels are already enforced by scripts/nhanh_etl.py before rows enter DB.
NHANH_LEAF_CATALOG: Tuple[Dict[str, str], ...] = (
    {"key": "FB_LPM_VIETNAM", "source_label": "FB - LPM Vietnam", "display_name": "Facebook LPM Vietnam"},
    {"key": "FB_LPM_VIETNAM_168", "source_label": "FB - LPM Vietnam 168", "display_name": "Facebook LPM Vietnam 168"},
    {"key": "FB_BLEDINA_BRASSES_VN", "source_label": "FB -Blédina Brasses VN", "display_name": "Facebook Blédina Brasses VN"},
    {"key": "LANDING_PAGE", "source_label": "landing page", "display_name": "Landing Page"},
    {"key": "WEBSITE_LPM", "source_label": "Web - lepetitmarseillais.vn", "display_name": "Website lepetitmarseillais.vn"},
    {"key": "ZALO_OA_LPM", "source_label": "OA - LPM Vietnam", "display_name": "Zalo OA LPM Vietnam"},
    {"key": "INSTAGRAM", "source_label": "INSTAGRAM", "display_name": "Instagram"},
)
NHANH_LABEL_TO_KEY = {row["source_label"]: row["key"] for row in NHANH_LEAF_CATALOG}
NHANH_KEY_TO_META = {row["key"]: dict(row) for row in NHANH_LEAF_CATALOG}
NHANH_INTERNAL_UNALLOCATED_KEY = "_UNALLOCATED"
NHANH_NO_BO_STATUSES: Tuple[str, ...] = ("Đã hủy", "Hệ Thống hủy", "Đã hoàn", "Khách hủy")



def json_default(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return str(value)


def clean(value: Any) -> Any:
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, tuple):
        return [clean(v) for v in value]
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    return value


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(number) or math.isinf(number):
        return default
    return number


def safe_int(value: Any) -> int:
    return int(round(safe_float(value)))


def ratio(numerator: Any, denominator: Any) -> Optional[float]:
    den = safe_float(denominator)
    if abs(den) < 1e-12:
        return None
    return safe_float(numerator) / den


def delta_pct(current: Any, previous: Any) -> Optional[float]:
    previous_value = safe_float(previous)
    if abs(previous_value) < 1e-12:
        return None
    return (safe_float(current) - previous_value) / previous_value


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(value)


def month_start(value: dt.date) -> dt.date:
    return dt.date(value.year, value.month, 1)


def add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    next_month = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    last_day = (next_month - dt.timedelta(days=1)).day
    return dt.date(year, month, min(value.day, last_day))


def date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    current = start
    while current <= end:
        yield current
        current += dt.timedelta(days=1)


def normalize_channel(value: Any) -> str:
    text = str(value or "").strip().upper()
    return {
        "DIGITAL": "NHANH",
        "NHANH.VN": "NHANH",
        "TIK TOK": "TIKTOK",
        "TIKTOK SHOP": "TIKTOK",
    }.get(text, text)


def connect():
    if psycopg2 is None:
        raise RuntimeError("Missing psycopg2. Install psycopg2-binary in the ETL environment.")
    dsn = os.getenv("DATABASE_URL")
    if dsn:
        return psycopg2.connect(dsn)
    return psycopg2.connect(
        host=os.getenv("PGHOST", os.getenv("ETL_DB_HOST", "localhost")),
        port=os.getenv("PGPORT", os.getenv("ETL_DB_PORT", "5433")),
        dbname=os.getenv("PGDATABASE", os.getenv("ETL_DB_NAME", "DuLieu")),
        user=os.getenv("PGUSER", os.getenv("ETL_DB_USER", "postgres")),
        password=os.getenv("PGPASSWORD", os.getenv("ETL_DB_PASSWORD", "")),
    )


def relation_exists(cur, schema: str, name: str) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL AS ok", (f"{schema}.{name}",))
    row = cur.fetchone()
    return bool(row and (row["ok"] if isinstance(row, Mapping) else row[0]))


def relation_columns(cur, schema: str, name: str) -> set[str]:
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
    return {str(r["column_name"] if isinstance(r, Mapping) else r[0]) for r in cur.fetchall()}


def fetch_all(cur, sql: str, params: Sequence[Any] = ()) -> List[Dict[str, Any]]:
    cur.execute(sql, tuple(params))
    return [clean(dict(row)) for row in cur.fetchall()]


def safe_json_numeric(json_column: str, key: str) -> str:
    key = key.replace("'", "''")
    text = f"NULLIF({json_column}->>'{key}', '')"
    return (
        f"CASE WHEN {text} ~ '^-?[0-9]+([.][0-9]+)?$' "
        f"THEN ({text})::numeric ELSE 0::numeric END"
    )


def sql_exact_cancelled(status_expr: str) -> str:
    """SQL predicate for the owner-approved exact cancelled status.

    Harmless normalization only: trim boundaries, collapse whitespace, lowercase.
    No accent folding and no substring matching. Therefore statuses such as
    "Đã huỷ", "Đã hủy bởi người mua", "cancelled", "refund", etc. are NOT
    cancelled unless the normalized value is exactly "đã hủy".
    """
    return (
        "lower(regexp_replace(btrim(COALESCE(" + status_expr + ",'')), "
        "'[[:space:]]+', ' ', 'g')) = 'đã hủy'"
    )

def load_growth_builder(script_dir: Path):
    path = script_dir / BASE_BUILDER
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}")
    spec = importlib.util.spec_from_file_location("hh_growth_v3_3", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "build_package"):
        raise RuntimeError(f"{path.name} has no build_package()")
    return module


def resolve_compare_date(
    report_date: dt.date,
    compare_mode: str,
    explicit_compare_date: Optional[str],
) -> dt.date:
    if explicit_compare_date:
        return parse_date(explicit_compare_date)
    if compare_mode == "previous_day":
        return report_date - dt.timedelta(days=1)
    if compare_mode == "previous_week_same_day":
        return report_date - dt.timedelta(days=7)
    if compare_mode == "previous_month_same_day":
        return add_months(report_date, -1)
    raise ValueError(f"Unsupported compare_mode: {compare_mode}")


def parse_vat_rates(raw: str) -> Dict[str, float]:
    if not raw.strip():
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("--vat-rates must be a JSON object")
    result: Dict[str, float] = {}
    for channel, value in payload.items():
        rate = safe_float(value, -1)
        if rate < 0 or rate > 1:
            raise ValueError(f"Invalid VAT rate for {channel}: {value}")
        result[normalize_channel(channel)] = rate
    return result


def fetch_scorecard_range(cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    if not relation_exists(cur, "control_tower", "v_scorecard_by_channel_daily_for_package"):
        raise RuntimeError("Missing control_tower.v_scorecard_by_channel_daily_for_package")
    return fetch_all(
        cur,
        """
        SELECT
            report_date::date AS report_date,
            channel::text AS channel,
            COALESCE(gross_sales,0)::numeric AS gross_sales,
            COALESCE(net_sales,0)::numeric AS net_sales,
            COALESCE(discount_voucher_return,0)::numeric AS sales_deductions_reported,
            COALESCE(cogs,0)::numeric AS cogs,
            COALESCE(platform_fees,0)::numeric AS platform_fees_reported,
            COALESCE(shipping_return_fee,0)::numeric AS shipping_return_fee,
            COALESCE(ads_cost,0)::numeric AS ads_cost,
            COALESCE(live_cost,0)::numeric AS live_cost,
            COALESCE(booking_fee,0)::numeric AS booking_fee,
            COALESCE(backoffice_fee,0)::numeric AS backoffice_fee,
            COALESCE(packaging_cost,0)::numeric AS packaging_cost,
            COALESCE(payout,0)::numeric AS payout,
            COALESCE(profit,0)::numeric AS legacy_profit,
            COALESCE(orders,0)::numeric AS orders,
            COALESCE(success_orders,0)::numeric AS success_orders,
            COALESCE(cancelled_orders,0)::numeric AS cancelled_orders,
            source_map
        FROM control_tower.v_scorecard_by_channel_daily_for_package
        WHERE report_date BETWEEN %s AND %s
        ORDER BY report_date, channel
        """,
        (start, end),
    )


def fetch_nhanh_cogs_split(cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    if not relation_exists(cur, "nhanh", "orders") or not relation_exists(cur, "nhanh", "order_items"):
        return []
    item_cols = relation_columns(cur, "nhanh", "order_items")
    gift_expr = "COALESCE(i.is_gift,false)" if "is_gift" in item_cols else "false"
    return fetch_all(
        cur,
        f"""
        SELECT
            o.order_date::date AS report_date,
            SUM(CASE WHEN NOT ({gift_expr})
                     THEN COALESCE(i.quantity,0)*COALESCE(i.unit_cogs,0)
                     ELSE 0 END)::numeric AS sold_cogs,
            SUM(CASE WHEN ({gift_expr})
                     THEN COALESCE(i.quantity,0)*COALESCE(i.unit_cogs,0)
                     ELSE 0 END)::numeric AS gift_cogs
        FROM nhanh.orders o
        LEFT JOIN nhanh.order_items i ON i.order_id=o.id
        WHERE o.order_date BETWEEN %s AND %s
        GROUP BY o.order_date::date
        """,
        (start, end),
    )



def fetch_nhanh_leaf_detail(cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Return Nhanh daily P&L components split by exact ``nhanh.orders.label``.

    The formulas deliberately mirror
    ``control_tower.v_scorecard_nhanh_daily_v2_12``:
      - gross = item quantity * item price;
      - net sales = exact ``nhanh.orders.order_value``;
      - COGS = item quantity * unit_cogs, split sold/gift;
      - shipping = ``nhanh.orders.shipping_fee``;
      - BO = 15% of positive order_value except the four exact v2.12 no-BO statuses;
      - packaging = 2,000 VND * item quantity;
      - ads = ``cost_vnd_vat`` when available, else ``cost_vnd``.

    Unknown/non-sales ad labels are retained in an internal ``_UNALLOCATED``
    bucket so the parent NHANH P&L still reconciles without inventing an
    allocation. That internal bucket is never rendered as a sales channel.
    """
    if not relation_exists(cur, "nhanh", "orders") or not relation_exists(cur, "nhanh", "order_items"):
        return []

    item_cols = relation_columns(cur, "nhanh", "order_items")
    gift_expr = "COALESCE(i.is_gift,false)" if "is_gift" in item_cols else "false"
    no_bo_sql = ", ".join("'" + x.replace("'", "''") + "'" for x in NHANH_NO_BO_STATUSES)

    sales_rows = fetch_all(
        cur,
        f"""
        WITH item AS (
            SELECT
                i.order_id,
                SUM(COALESCE(i.quantity,0) * COALESCE(i.price,0))::numeric AS gross_sales,
                SUM(CASE WHEN NOT ({gift_expr})
                         THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0)
                         ELSE 0 END)::numeric AS sold_cogs,
                SUM(CASE WHEN ({gift_expr})
                         THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0)
                         ELSE 0 END)::numeric AS gift_cogs,
                SUM(COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0))::numeric AS cogs_total,
                SUM(COALESCE(i.quantity,0))::numeric AS qty
            FROM nhanh.order_items i
            GROUP BY i.order_id
        )
        SELECT
            o.order_date::date AS report_date,
            BTRIM(COALESCE(o.label,''))::text AS source_label,
            SUM(COALESCE(i.gross_sales,0))::numeric AS gross_sales,
            SUM(COALESCE(o.order_value,0))::numeric AS net_sales,
            SUM(COALESCE(i.sold_cogs,0))::numeric AS sold_cogs,
            SUM(COALESCE(i.gift_cogs,0))::numeric AS gift_cogs,
            SUM(COALESCE(i.cogs_total,0))::numeric AS cogs_total,
            SUM(COALESCE(o.shipping_fee,0))::numeric AS shipping_return_fee,
            SUM(CASE
                    WHEN BTRIM(COALESCE(o.status,'')) IN ({no_bo_sql}) THEN 0
                    ELSE GREATEST(COALESCE(o.order_value,0),0) * 0.15
                END)::numeric AS backoffice_fee,
            SUM(COALESCE(i.qty,0) * 2000)::numeric AS packaging_cost,
            COUNT(*)::bigint AS orders,
            COUNT(*) FILTER (WHERE BTRIM(COALESCE(o.status,'')) NOT IN ({no_bo_sql}))::bigint AS success_orders,
            COUNT(*) FILTER (WHERE BTRIM(COALESCE(o.status,'')) IN ({no_bo_sql}))::bigint AS cancelled_orders
        FROM nhanh.orders o
        LEFT JOIN item i ON i.order_id=o.id
        WHERE o.order_date BETWEEN %s AND %s
        GROUP BY o.order_date::date, BTRIM(COALESCE(o.label,''))
        ORDER BY o.order_date::date, source_label
        """,
        (start, end),
    )

    ads_rows: List[Dict[str, Any]] = []
    if relation_exists(cur, "nhanh", "ads_costs"):
        ads_cols = relation_columns(cur, "nhanh", "ads_costs")
        if "cost_vnd_vat" in ads_cols and "cost_vnd" in ads_cols:
            ads_expr = "COALESCE(cost_vnd_vat, cost_vnd, 0)"
        elif "cost_vnd_vat" in ads_cols:
            ads_expr = "COALESCE(cost_vnd_vat, 0)"
        else:
            ads_expr = "COALESCE(cost_vnd, 0)"
        ads_rows = fetch_all(
            cur,
            f"""
            SELECT
                ads_date::date AS report_date,
                BTRIM(COALESCE(shop_label,'DEFAULT'))::text AS source_label,
                SUM({ads_expr})::numeric AS ads_cost
            FROM nhanh.ads_costs
            WHERE ads_date BETWEEN %s AND %s
            GROUP BY ads_date::date, BTRIM(COALESCE(shop_label,'DEFAULT'))
            ORDER BY ads_date::date, source_label
            """,
            (start, end),
        )

    numeric_fields = (
        "gross_sales", "net_sales", "sold_cogs", "gift_cogs", "cogs_total",
        "shipping_return_fee", "backoffice_fee", "packaging_cost", "ads_cost",
        "orders", "success_orders", "cancelled_orders",
    )
    acc: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add_row(row: Mapping[str, Any], *, is_ads: bool = False) -> None:
        day = str(row.get("report_date"))
        label = str(row.get("source_label") or "").strip()
        leaf_key = NHANH_LABEL_TO_KEY.get(label, NHANH_INTERNAL_UNALLOCATED_KEY)
        key = (day, leaf_key)
        out = acc.setdefault(key, {
            "report_date": day,
            "leaf_channel": leaf_key,
            "source_label": NHANH_KEY_TO_META.get(leaf_key, {}).get("source_label", "UNALLOCATED"),
            "source_labels": [],
            **{field: 0.0 for field in numeric_fields},
        })
        if label and label not in out["source_labels"]:
            out["source_labels"].append(label)
        if is_ads:
            out["ads_cost"] += safe_float(row.get("ads_cost"))
        else:
            for field in numeric_fields:
                if field != "ads_cost":
                    out[field] += safe_float(row.get(field))

    for row in sales_rows:
        add_row(row)
    for row in ads_rows:
        add_row(row, is_ads=True)

    return [clean(acc[k]) for k in sorted(acc)]


def lookup_nhanh_leaf(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for row in rows:
        leaf = str(row.get("leaf_channel") or NHANH_INTERNAL_UNALLOCATED_KEY)
        day = str(row.get("report_date") or "")
        if day:
            result.setdefault(leaf, {})[day] = dict(row)
    return result


def nhanh_leaf_base(day: dt.date, detail: Mapping[str, Any]) -> Dict[str, Any]:
    base = blank_base(day, "NHANH")
    cogs = safe_float(detail.get("cogs_total"))
    shipping = safe_float(detail.get("shipping_return_fee"))
    ads = safe_float(detail.get("ads_cost"))
    backoffice = safe_float(detail.get("backoffice_fee"))
    packaging = safe_float(detail.get("packaging_cost"))
    net = safe_float(detail.get("net_sales"))
    base.update({
        "gross_sales": safe_float(detail.get("gross_sales")),
        "net_sales": net,
        "cogs": cogs,
        "shipping_return_fee": shipping,
        "ads_cost": ads,
        "backoffice_fee": backoffice,
        "packaging_cost": packaging,
        "legacy_profit": net - cogs - shipping - ads - backoffice - packaging,
        "orders": safe_int(detail.get("orders")),
        "success_orders": safe_int(detail.get("success_orders")),
        "cancelled_orders": safe_int(detail.get("cancelled_orders")),
        "source_map": {
            "leaf_split": "nhanh.orders.label exact",
            "gross_sales": "nhanh.order_items.quantity * price",
            "net_sales": "nhanh.orders.order_value",
            "ads_cost": "nhanh.ads_costs grouped by shop_label; unknown labels kept in hidden _UNALLOCATED bucket",
        },
    })
    return base


def has_sales(row: Mapping[str, Any]) -> bool:
    """Sales visibility rule: only revenue amounts decide visibility, not costs/orders."""
    return abs(safe_float(row.get("gross_sales"))) > EPSILON or abs(safe_float(row.get("net_sales"))) > EPSILON


def fetch_shopee_detail(cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Shopee direct detail under the exact-status cancellation contract.

    Cancellation source of truth: normalized status == "đã hủy" only.
    Return/refund classification is disabled. Every other status is processed
    as a normal non-cancelled order.
    Gross Sales includes every order status and excludes gift rows only through
    the ETL-produced order gross.
    """
    if not relation_exists(cur, "shopee", "orders"):
        return []
    item_exists = relation_exists(cur, "shopee", "order_items")
    extra_exists = relation_exists(cur, "shopee", "order_financials_extra")

    cancelled = f"({sql_exact_cancelled('o.status')})"
    active = f"(NOT {cancelled})"

    if item_exists:
        item_cols = relation_columns(cur, "shopee", "order_items")
        gift_expr = "COALESCE(i.is_gift,false)" if "is_gift" in item_cols else "false"
        cancelled_o2 = f"({sql_exact_cancelled('o2.status')})"
        item_cte = f"""
        item AS (
            SELECT
                i.order_id,
                SUM(CASE WHEN NOT ({gift_expr})
                         THEN COALESCE(i.quantity,0)*COALESCE(i.unit_cogs,0)
                         ELSE 0 END)::numeric AS sold_cogs,
                SUM(CASE WHEN ({gift_expr})
                         THEN COALESCE(i.quantity,0)*COALESCE(i.unit_cogs,0)
                         ELSE 0 END)::numeric AS gift_cogs,
                SUM(CASE WHEN NOT {cancelled_o2}
                         THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS non_cancelled_qty
            FROM shopee.order_items i
            JOIN shopee.orders o2 ON o2.id=i.order_id
            GROUP BY i.order_id
        )
        """
        item_join = "LEFT JOIN item i ON i.order_id=o.id"
        sold_cogs = "SUM(COALESCE(i.sold_cogs,0))::numeric"
        gift_cogs = "SUM(COALESCE(i.gift_cogs,0))::numeric"
        packaging = "SUM(COALESCE(i.non_cancelled_qty,0)*2000)::numeric"
    else:
        item_cte = "item AS (SELECT NULL::bigint AS order_id WHERE false)"
        item_join = "LEFT JOIN item i ON false"
        sold_cogs = "0::numeric"
        gift_cogs = "0::numeric"
        packaging = "0::numeric"

    if extra_exists:
        extra_join = (
            "LEFT JOIN shopee.order_financials_extra e "
            "ON e.external_order_id=o.external_order_id AND e.shop_label=o.shop_label"
        )
        gross_expr = "COALESCE(e.gross_sales,o.gross_sales,0)"
        seller_discount = f"SUM(CASE WHEN {active} THEN COALESCE(e.seller_discount,0) ELSE 0 END)::numeric"
        # IMPORTANT: despite the legacy DB column name, e.platform_voucher comes from
        # raw Shopee "Mã giảm giá của Shop" / "shop voucher" and is seller-funded.
        # True platform-funded vouchers are not deducted by this package.
        seller_voucher = f"SUM(CASE WHEN {active} THEN COALESCE(e.platform_voucher,0) ELSE 0 END)::numeric"
    else:
        extra_join = ""
        gross_expr = "COALESCE(o.gross_sales,0)"
        seller_discount = "0::numeric"
        seller_voucher = "0::numeric"

    return fetch_all(
        cur,
        f"""
        WITH {item_cte}
        SELECT
            o.order_date::date AS report_date,
            SUM({gross_expr})::numeric AS gross_all_orders,
            SUM(CASE WHEN {active} THEN {gross_expr} ELSE 0 END)::numeric AS gross_non_cancelled_orders,
            SUM(CASE WHEN {cancelled} THEN {gross_expr} ELSE 0 END)::numeric AS gross_cancelled_orders,
            COUNT(*)::numeric AS order_count_detail,
            COUNT(*) FILTER (WHERE {active})::numeric AS non_cancelled_order_count_detail,
            COUNT(*) FILTER (WHERE {cancelled})::numeric AS cancelled_order_count_detail,
            COUNT(*) FILTER (WHERE {cancelled} AND {gross_expr} <> 0)::numeric AS cancelled_orders_with_gross,
            COUNT(*) FILTER (WHERE COALESCE(o.affiliate_amount,0) <> 0)::numeric AS affiliate_order_count_detail,
            SUM(COALESCE(o.fixed_fee,0))::numeric AS fixed_fee,
            SUM(COALESCE(o.service_fee,0))::numeric AS service_fee,
            SUM(COALESCE(o.payment_fee,0))::numeric AS payment_fee,
            SUM(COALESCE(o.affiliate_amount,0))::numeric AS affiliate_total,
            SUM(COALESCE(o.return_fee,0))::numeric AS cancel_related_shipping_fee,
            {sold_cogs} AS sold_cogs,
            {gift_cogs} AS gift_cogs,
            {packaging} AS packaging_calculated,
            {seller_discount} AS seller_discount,
            {seller_voucher} AS seller_voucher
        FROM shopee.orders o
        {item_join}
        {extra_join}
        WHERE o.order_date BETWEEN %s AND %s
        GROUP BY o.order_date::date
        """,
        (start, end),
    )

def fetch_tiktok_detail(cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """TikTok direct detail under the exact-status cancellation contract.

    Only normalized order_status == "đã hủy" is cancelled. Return/refund classification is disabled; every other status is normal for this package.
    """
    if not relation_exists(cur, "tiktok", "orders_pnl"):
        return []
    cols = relation_columns(cur, "tiktok", "orders_pnl")

    def c(name: str) -> str:
        return f"COALESCE(o.{name},0)::numeric" if name in cols else "0::numeric"

    creator = safe_json_numeric("o.raw_payload", "comm_creator") if "raw_payload" in cols else "0::numeric"
    partner = safe_json_numeric("o.raw_payload", "comm_partner") if "raw_payload" in cols else "0::numeric"
    cancelled = f"({sql_exact_cancelled('o.order_status')})"
    active = f"(NOT {cancelled})"

    item_exists = relation_exists(cur, "tiktok", "order_items")
    cancelled_o2 = f"({sql_exact_cancelled('o2.order_status')})"
    if item_exists:
        item_cte = f"""
        item_gross AS (
            SELECT
                i.external_order_id,
                i.shop_label,
                SUM(COALESCE(i.quantity,0) * COALESCE(i.price,0))::numeric AS gross_all_order,
                SUM(CASE
                        WHEN NOT ({cancelled_o2}) AND COALESCE(i.price,0) <> 0
                        THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0)
                        ELSE 0
                    END)::numeric AS sold_cogs,
                SUM(CASE
                        WHEN NOT ({cancelled_o2}) AND COALESCE(i.price,0) = 0
                        THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0)
                        ELSE 0
                    END)::numeric AS gift_cogs
            FROM tiktok.order_items i
            JOIN tiktok.orders_pnl o2
              ON o2.external_order_id=i.external_order_id
             AND o2.shop_label=i.shop_label
            GROUP BY i.external_order_id, i.shop_label
        )
        """
        item_join = (
            "LEFT JOIN item_gross g ON g.external_order_id=o.external_order_id "
            "AND g.shop_label=o.shop_label"
        )
        gross_expr = "COALESCE(g.gross_all_order,o.gmv_before_cancel,o.fee_base_revenue,0)"
    else:
        item_cte = "item_gross AS (SELECT NULL::text AS external_order_id, NULL::text AS shop_label, 0::numeric AS gross_all_order, 0::numeric AS sold_cogs, 0::numeric AS gift_cogs WHERE false)"
        item_join = "LEFT JOIN item_gross g ON false"
        gross_expr = "COALESCE(o.gmv_before_cancel,o.fee_base_revenue,0)"

    fee_base = c('fee_base_revenue')
    return fetch_all(
        cur,
        f"""
        WITH {item_cte}
        SELECT
            o.order_date::date AS report_date,
            SUM({gross_expr})::numeric AS gross_all_orders,
            SUM(CASE WHEN {active} THEN {gross_expr} ELSE 0 END)::numeric AS gross_non_cancelled_orders,
            SUM(CASE WHEN {cancelled} THEN {gross_expr} ELSE 0 END)::numeric AS gross_cancelled_orders,
            COUNT(*)::numeric AS order_count_detail,
            COUNT(*) FILTER (WHERE {active})::numeric AS non_cancelled_order_count_detail,
            COUNT(*) FILTER (WHERE {cancelled})::numeric AS cancelled_order_count_detail,
            COUNT(*) FILTER (WHERE {cancelled} AND {gross_expr} <> 0)::numeric AS cancelled_orders_with_gross,
            COUNT(*) FILTER (WHERE {c('commission_amount')} <> 0)::numeric AS affiliate_order_count_detail,
            SUM(CASE WHEN {active} THEN GREATEST(({gross_expr}) - ({fee_base}),0) ELSE 0 END)::numeric AS seller_discount,
            0::numeric AS seller_voucher,
            SUM(COALESCE(g.sold_cogs,0))::numeric AS sold_cogs,
            SUM(COALESCE(g.gift_cogs,0))::numeric AS gift_cogs,
            SUM({c('fixed_fee')})::numeric AS fixed_fee,
            SUM({c('payment_fee')})::numeric AS payment_fee,
            SUM({c('vxp_fee')})::numeric AS vxp_fee,
            SUM({c('infrastructure_fee')})::numeric AS infrastructure_fee,
            SUM({c('commission_amount')})::numeric AS affiliate_total,
            SUM({creator})::numeric AS affiliate_creator,
            SUM({partner})::numeric AS affiliate_partner,
            SUM({c('booking_fee')})::numeric AS booking_kol_koc,
            SUM({c('return_shipping_fee')})::numeric AS cancel_related_shipping_fee,
            SUM({c('packaging_cost')})::numeric AS packaging_calculated,
            SUM({c('backoffice_cost')})::numeric AS backoffice_direct,
            SUM({c('settlement_paid')})::numeric AS settlement_paid,
            SUM({c('settlement_unpaid')})::numeric AS settlement_unpaid,
            SUM({c('estimated_payout')})::numeric AS estimated_payout
        FROM tiktok.orders_pnl o
        {item_join}
        WHERE o.order_date BETWEEN %s AND %s
        GROUP BY o.order_date::date
        """,
        (start, end),
    )

def fetch_marketplace_status_audit(cur, start: dt.date, end: dt.date) -> Dict[str, Any]:
    """Audit exact-status classification and stale DB is_cancelled flags."""
    result: Dict[str, Any] = {}
    specs = {
        "SHOPEE": ("shopee", "orders", "status"),
        "TIKTOK": ("tiktok", "orders_pnl", "order_status"),
    }
    for channel, (schema, table, status_col) in specs.items():
        if not relation_exists(cur, schema, table):
            result[channel] = {"available": False, "status_rows": [], "flag_mismatch_count": 0}
            continue
        pred = sql_exact_cancelled(f"o.{status_col}")
        rows = fetch_all(
            cur,
            f"""
            SELECT
                COALESCE(NULLIF(btrim(o.{status_col}),''), '<BLANK>') AS raw_status,
                ({pred}) AS classified_cancelled,
                COUNT(*)::numeric AS orders
            FROM {schema}.{table} o
            WHERE o.order_date BETWEEN %s AND %s
            GROUP BY 1,2
            ORDER BY classified_cancelled DESC, orders DESC, raw_status
            """,
            (start, end),
        )
        mismatch = fetch_all(
            cur,
            f"""
            SELECT COUNT(*)::numeric AS mismatch_count
            FROM {schema}.{table} o
            WHERE o.order_date BETWEEN %s AND %s
              AND COALESCE(o.is_cancelled,false) <> ({pred})
            """,
            (start, end),
        )
        result[channel] = {
            "available": True,
            "rule": 'normalized status == "đã hủy"',
            "return_refund_classification": "DISABLED",
            "status_rows": rows,
            "flag_mismatch_count": safe_int(mismatch[0].get("mismatch_count")) if mismatch else 0,
        }
    return clean(result)

def fetch_ads_and_live(cur, channel: str, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    if channel == "SHOPEE" and relation_exists(cur, "shopee", "ads_costs"):
        cols = relation_columns(cur, "shopee", "ads_costs")
        cost = "COALESCE(cost_vnd_vat,cost_vnd,0)" if "cost_vnd_vat" in cols else "COALESCE(cost_vnd,0)"
        for row in fetch_all(
            cur,
            f"""
            SELECT ads_date::date AS report_date,
                   SUM({cost})::numeric AS ads_other
            FROM shopee.ads_costs
            WHERE ads_date BETWEEN %s AND %s
            GROUP BY ads_date::date
            """,
            (start, end),
        ):
            rows[str(row["report_date"])] = row

    if channel == "TIKTOK":
        if relation_exists(cur, "tiktok", "ads_costs"):
            cols = relation_columns(cur, "tiktok", "ads_costs")
            cost = "COALESCE(cost_vnd_vat,cost_vnd,0)" if "cost_vnd_vat" in cols else "COALESCE(cost_vnd,0)"
            ads_type = "UPPER(COALESCE(ads_type,''))" if "ads_type" in cols else "''"
            for row in fetch_all(
                cur,
                f"""
                SELECT
                    ads_date::date AS report_date,
                    SUM(CASE WHEN {ads_type}='PRODUCT' THEN {cost} ELSE 0 END)::numeric AS ads_product,
                    SUM(CASE WHEN {ads_type}='LIVE' THEN {cost} ELSE 0 END)::numeric AS ads_live,
                    SUM(CASE WHEN {ads_type} NOT IN ('PRODUCT','LIVE') THEN {cost} ELSE 0 END)::numeric AS ads_other_source
                FROM tiktok.ads_costs
                WHERE ads_date BETWEEN %s AND %s
                GROUP BY ads_date::date
                """,
                (start, end),
            ):
                rows[str(row["report_date"])] = row

        if relation_exists(cur, "tiktok", "live_costs"):
            cols = relation_columns(cur, "tiktok", "live_costs")
            in_house = "COALESCE(in_house_cost,0)" if "in_house_cost" in cols else "0"
            teaser = "COALESCE(teaser_cost,0)" if "teaser_cost" in cols else "0"
            for row in fetch_all(
                cur,
                f"""
                SELECT
                    live_date::date AS report_date,
                    SUM({in_house})::numeric AS livestream_inhouse,
                    SUM({teaser})::numeric AS teaser_campaign
                FROM tiktok.live_costs
                WHERE live_date BETWEEN %s AND %s
                GROUP BY live_date::date
                """,
                (start, end),
            ):
                key = str(row["report_date"])
                rows.setdefault(key, {"report_date": row["report_date"]})
                rows[key].update(row)

    return [clean(rows[key]) for key in sorted(rows)]


def lookup(rows: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(r["report_date"]): dict(r) for r in rows if r.get("report_date")}


def merge_lookups(*maps: Mapping[str, Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    keys = set().union(*(m.keys() for m in maps))
    result: Dict[str, Dict[str, Any]] = {}
    for key in keys:
        merged: Dict[str, Any] = {"report_date": key}
        for m in maps:
            if key in m:
                merged.update(m[key])
        result[key] = merged
    return result


def blank_base(report_date: dt.date, channel: str) -> Dict[str, Any]:
    return {
        "report_date": report_date.isoformat(),
        "channel": channel,
        "gross_sales": 0,
        "net_sales": 0,
        "sales_deductions_reported": 0,
        "cogs": 0,
        "platform_fees_reported": 0,
        "shipping_return_fee": 0,
        "ads_cost": 0,
        "live_cost": 0,
        "booking_fee": 0,
        "backoffice_fee": 0,
        "packaging_cost": 0,
        "payout": 0,
        "legacy_profit": 0,
        "orders": 0,
        "success_orders": 0,
        "cancelled_orders": 0,
        "source_map": {},
    }


APPLICABLE = {
    "GT": {"sold_cogs", "logistics_order_cost", "ads_other", "backoffice_cost"},
    "MT": {"sold_cogs", "logistics_order_cost", "ads_other", "backoffice_cost"},
    "NHANH": {
        "sold_cogs", "gift_cogs", "logistics_order_cost",
        "packaging_cost", "ads_other", "backoffice_cost",
    },
    "SHOPEE": {
        "gross_cancelled_sales", "seller_discount",
        "seller_voucher", "other_sales_adjustment", "sold_cogs", "gift_cogs",
        "fixed_fee", "service_fee", "payment_fee",
        "cancel_related_shipping_fee", "packaging_cost", "ads_other",
        "affiliate_total", "backoffice_cost",
    },
    "TIKTOK": {
        "gross_cancelled_sales", "seller_discount",
        "other_sales_adjustment", "sold_cogs", "gift_cogs", "fixed_fee",
        "payment_fee", "vxp_fee", "infrastructure_fee", "cancel_related_shipping_fee",
        "packaging_cost", "ads_product", "ads_live", "teaser_campaign", "ads_other",
        "affiliate_creator", "affiliate_partner", "affiliate_other", "affiliate_total",
        "booking_kol_koc", "livestream_inhouse", "backoffice_cost",
    },
}

def apply_vat(value: float, channel: str, vat_rates: Mapping[str, float]) -> Tuple[float, float]:
    rate = safe_float(vat_rates.get(channel, 0))
    return (value / (1 + rate), rate) if rate else (value, 0.0)


def compose_pnl(
    base: Mapping[str, Any],
    detail: Mapping[str, Any],
    vat_rates: Mapping[str, float],
) -> Dict[str, Any]:
    channel = normalize_channel(base.get("channel"))

    use_detail_gross = channel in {"SHOPEE", "TIKTOK"} and "gross_all_orders" in detail
    gross_source = safe_float(detail.get("gross_all_orders")) if use_detail_gross else safe_float(base.get("gross_sales"))
    net_source = safe_float(base.get("net_sales"))
    gross_sales, vat_rate = apply_vat(gross_source, channel, vat_rates)
    net_sales, _ = apply_vat(net_source, channel, vat_rates)

    gross_non_cancel_source = safe_float(detail.get("gross_non_cancelled_orders"), gross_source)
    gross_cancel_source = safe_float(detail.get("gross_cancelled_orders"))
    gross_non_cancelled_sales, _ = apply_vat(gross_non_cancel_source, channel, vat_rates)
    gross_cancelled_sales, _ = apply_vat(gross_cancel_source, channel, vat_rates)
    # Contract v4.4: only exact "Đã hủy" is cancelled. Every other status is normal.
    # Therefore Gross has exactly two mutually-exclusive buckets.
    gross_status_reconciliation_gap = gross_sales - (gross_non_cancelled_sales + gross_cancelled_sales)
    if abs(gross_status_reconciliation_gap) < EPSILON:
        gross_status_reconciliation_gap = 0.0

    reported_deductions_source = safe_float(base.get("sales_deductions_reported"))
    reported_deductions, _ = apply_vat(reported_deductions_source, channel, vat_rates)
    seller_discount_source = safe_float(detail.get("seller_discount"))
    # Shopee legacy DB field e.platform_voucher is actually raw "Mã giảm giá của Shop".
    # Treat it only as seller-funded Shop voucher. Platform-funded vouchers are ignored.
    seller_voucher_source = safe_float(detail.get("seller_voucher"))
    seller_discount, _ = apply_vat(seller_discount_source, channel, vat_rates)
    seller_voucher, _ = apply_vat(seller_voucher_source, channel, vat_rates)

    sales_deductions_total = gross_sales - net_sales
    other_sales_adjustment = sales_deductions_total - (
        gross_cancelled_sales + seller_discount + seller_voucher
    )
    if abs(other_sales_adjustment) < EPSILON:
        other_sales_adjustment = 0.0

    cogs_total = safe_float(base.get("cogs"))
    cogs_split_available = ("sold_cogs" in detail) or ("gift_cogs" in detail)
    sold_cogs = safe_float(detail.get("sold_cogs"))
    gift_cogs = safe_float(detail.get("gift_cogs"))
    if channel in {"GT", "MT"} and not cogs_split_available:
        # GT/MT preserve the legacy total COGS contract. The sold/gift split is
        # required only for Nhanh/Shopee/TikTok in v4.5.
        sold_cogs = cogs_total
        gift_cogs = 0.0
    cogs_split_reconciliation_gap = cogs_total - sold_cogs - gift_cogs
    if abs(cogs_split_reconciliation_gap) < EPSILON:
        cogs_split_reconciliation_gap = 0.0

    fixed_fee = safe_float(detail.get("fixed_fee"))
    service_fee = safe_float(detail.get("service_fee"))
    payment_fee = safe_float(detail.get("payment_fee"))
    vxp_fee = safe_float(detail.get("vxp_fee"))
    infrastructure_fee = safe_float(detail.get("infrastructure_fee"))
    fulfillment_fee = safe_float(detail.get("fulfillment_fee"))
    packaging_cost = safe_float(base.get("packaging_cost")) or safe_float(detail.get("packaging_calculated"))

    logistics_order_cost = safe_float(base.get("shipping_return_fee"))
    cancel_related_shipping_fee = safe_float(detail.get("cancel_related_shipping_fee"))
    if channel in {"SHOPEE", "TIKTOK"}:
        cancel_related_shipping_fee = cancel_related_shipping_fee or logistics_order_cost
        logistics_order_cost = 0.0

    ads_base = safe_float(base.get("ads_cost"))
    ads_product = safe_float(detail.get("ads_product"))
    ads_live = safe_float(detail.get("ads_live"))
    teaser_campaign = safe_float(detail.get("teaser_campaign"))
    if channel == "TIKTOK":
        detailed_ads_known = ads_product + ads_live + teaser_campaign
        ads_other = max(ads_base - detailed_ads_known, 0.0)
        if abs(ads_other) < EPSILON:
            ads_other = 0.0
        ads_reconciliation_gap = ads_base - (detailed_ads_known + ads_other)
    else:
        ads_product = ads_live = teaser_campaign = 0.0
        ads_other = safe_float(detail.get("ads_other")) or ads_base
        ads_reconciliation_gap = ads_base - ads_other

    affiliate_total_source = safe_float(detail.get("affiliate_total"))
    affiliate_creator = safe_float(detail.get("affiliate_creator"))
    affiliate_partner = safe_float(detail.get("affiliate_partner"))
    if channel == "SHOPEE":
        affiliate_creator = affiliate_partner = affiliate_other = 0.0
        affiliate_total = affiliate_total_source
        affiliate_reconciliation_gap = 0.0
    else:
        classified_affiliate = affiliate_creator + affiliate_partner
        affiliate_other = max(affiliate_total_source - classified_affiliate, 0.0)
        if abs(affiliate_other) < EPSILON:
            affiliate_other = 0.0
        affiliate_total = affiliate_creator + affiliate_partner + affiliate_other
        affiliate_reconciliation_gap = affiliate_total_source - affiliate_total

    booking_kol_koc = safe_float(detail.get("booking_kol_koc")) or safe_float(base.get("booking_fee"))
    livestream_inhouse = safe_float(detail.get("livestream_inhouse")) or safe_float(base.get("live_cost"))
    content_cost = safe_float(detail.get("content_cost"))
    trade_marketing_cost = safe_float(detail.get("trade_marketing_cost"))
    agency_campaign_direct_cost = safe_float(detail.get("agency_campaign_direct_cost"))
    backoffice_cost = safe_float(base.get("backoffice_fee")) or safe_float(detail.get("backoffice_direct"))

    gm1 = net_sales - cogs_total
    cm1_cost_total = sum([
        fixed_fee, service_fee, payment_fee, vxp_fee, infrastructure_fee,
        fulfillment_fee, packaging_cost, logistics_order_cost, cancel_related_shipping_fee,
    ])
    cm1 = gm1 - cm1_cost_total
    cm2_cost_total = sum([
        ads_product, ads_live, teaser_campaign, ads_other, affiliate_total,
        booking_kol_koc, livestream_inhouse, content_cost,
        trade_marketing_cost, agency_campaign_direct_cost,
    ])
    cm2 = cm1 - cm2_cost_total
    profit = cm2 - backoffice_cost

    component_platform_ex_affiliate = (
        fixed_fee + service_fee + payment_fee + vxp_fee + infrastructure_fee
    )
    component_platform_plus_affiliate = component_platform_ex_affiliate + affiliate_total_source
    platform_reported = safe_float(base.get("platform_fees_reported"))
    gap_if_excludes_affiliate = platform_reported - component_platform_ex_affiliate
    gap_if_includes_affiliate = platform_reported - component_platform_plus_affiliate
    legacy_platform_includes_affiliate = abs(gap_if_includes_affiliate) <= abs(gap_if_excludes_affiliate)
    expected_legacy_platform = component_platform_plus_affiliate if legacy_platform_includes_affiliate else component_platform_ex_affiliate
    legacy_profit = safe_float(base.get("legacy_profit"))

    if channel in {"SHOPEE", "TIKTOK"} and "order_count_detail" in detail:
        orders = safe_int(detail.get("order_count_detail"))
        cancelled_orders = safe_int(detail.get("cancelled_order_count_detail"))
        success_orders = max(orders - cancelled_orders, 0)
    else:
        orders = safe_int(base.get("orders"))
        cancelled_orders = safe_int(base.get("cancelled_orders"))
        success_orders = safe_int(base.get("success_orders"))

    return clean({
        "report_date": str(base.get("report_date")), "channel": channel, "vat_rate": vat_rate,
        "gross_sales_source": gross_source, "gross_sales": gross_sales,
        "gross_non_cancelled_sales": gross_non_cancelled_sales,
        "gross_cancelled_sales": gross_cancelled_sales,
        "gross_status_reconciliation_gap": gross_status_reconciliation_gap,
        "net_sales_source": net_source,
        "seller_discount_source": seller_discount_source,
        "seller_voucher_source": seller_voucher_source,
        "seller_discount": seller_discount, "seller_voucher": seller_voucher,
        "other_sales_adjustment": other_sales_adjustment,
        "sales_deductions_reported_source": reported_deductions_source,
        "sales_deductions_reported": reported_deductions,
        "sales_deductions_total": sales_deductions_total,
        "sales_deductions_component_sum": gross_cancelled_sales + seller_discount + seller_voucher + other_sales_adjustment,
        "sales_deductions_reconciliation_gap": sales_deductions_total - (gross_cancelled_sales + seller_discount + seller_voucher + other_sales_adjustment),
        "net_sales": net_sales,
        "orders": orders, "success_orders": success_orders,
        "cancelled_orders": cancelled_orders,
        "cancellation_rate": ratio(cancelled_orders, orders),
        "cancelled_orders_with_gross": safe_int(detail.get("cancelled_orders_with_gross")),
        "sold_cogs": sold_cogs, "gift_cogs": gift_cogs,
        "cogs_total": cogs_total, "cogs_split_available": cogs_split_available,
        "cogs_split_reconciliation_gap": cogs_split_reconciliation_gap,
        "gm1": gm1, "gm1_margin": ratio(gm1, net_sales),
        "fixed_fee": fixed_fee, "service_fee": service_fee, "payment_fee": payment_fee,
        "vxp_fee": vxp_fee, "infrastructure_fee": infrastructure_fee,
        "fulfillment_fee": fulfillment_fee, "packaging_cost": packaging_cost,
        "logistics_order_cost": logistics_order_cost,
        "cancel_related_shipping_fee": cancel_related_shipping_fee,
        "cm1_cost_total": cm1_cost_total, "cm1": cm1, "cm1_margin": ratio(cm1, net_sales),
        "ads_product": ads_product, "ads_live": ads_live, "teaser_campaign": teaser_campaign,
        "ads_other": ads_other, "ads_total": ads_product + ads_live + teaser_campaign + ads_other,
        "ads_cost_reported_legacy": ads_base, "ads_reconciliation_gap": ads_reconciliation_gap,
        "affiliate_creator": affiliate_creator, "affiliate_partner": affiliate_partner,
        "affiliate_other": affiliate_other, "affiliate_total": affiliate_total,
        "affiliate_total_source": affiliate_total_source,
        "affiliate_reconciliation_gap": affiliate_reconciliation_gap,
        "affiliate_order_count_detail": safe_int(detail.get("affiliate_order_count_detail")),
        "booking_kol_koc": booking_kol_koc, "livestream_inhouse": livestream_inhouse,
        "content_cost": content_cost, "trade_marketing_cost": trade_marketing_cost,
        "agency_campaign_direct_cost": agency_campaign_direct_cost,
        "cm2_cost_total": cm2_cost_total, "cm2": cm2, "cm2_margin": ratio(cm2, net_sales),
        "backoffice_cost": backoffice_cost, "profit": profit, "profit_margin": ratio(profit, net_sales),
        "platform_fees_reported_legacy": platform_reported,
        "platform_components_ex_affiliate": component_platform_ex_affiliate,
        "platform_components_plus_affiliate": component_platform_plus_affiliate,
        "legacy_platform_includes_affiliate": legacy_platform_includes_affiliate,
        "platform_expected_legacy": expected_legacy_platform,
        "platform_reconciliation_gap": platform_reported - expected_legacy_platform,
        "legacy_profit": legacy_profit, "profit_vs_legacy_profit": profit - legacy_profit,
        "payout": safe_float(base.get("payout")),
        "settlement_paid": safe_float(detail.get("settlement_paid")),
        "settlement_unpaid": safe_float(detail.get("settlement_unpaid")),
        "estimated_payout": safe_float(detail.get("estimated_payout")),
        "applicable_cost_codes": sorted(APPLICABLE.get(channel, set())),
        "source_map": base.get("source_map") or {},
    })

LINE_DEFINITIONS = (
    ("gross_sales", "Gross Sales — toàn bộ trạng thái", "REVENUE", False),
    ("gross_non_cancelled_sales", "Trong đó: doanh số đơn không hủy", "REVENUE_DETAIL", False),
    ("gross_cancelled_sales", "Doanh số đơn ĐÃ HỦY", "REVENUE_BRIDGE", True),
    ("seller_discount", "Seller discount/khuyến mại người bán", "REVENUE_BRIDGE", True),
    ("seller_voucher", "Voucher Shop do người bán chịu", "REVENUE_BRIDGE", True),
    ("other_sales_adjustment", "Điều chỉnh doanh thu khác (+/-)", "REVENUE_BRIDGE", True),
    ("sales_deductions_total", "Tổng giảm trừ doanh thu", "REVENUE_SUBTOTAL", True),
    ("net_sales", "Net Sales", "REVENUE", False),
    ("sold_cogs", "Giá vốn hàng bán", "GM1", True),
    ("gift_cogs", "Giá vốn hàng tặng/khuyến mại", "GM1", True),
    ("gm1", "GM1", "GM1", False),
    ("fixed_fee", "Phí sàn/fixed fee", "CM1", True),
    ("service_fee", "Phí dịch vụ", "CM1", True),
    ("payment_fee", "Phí payment/xử lý giao dịch", "CM1", True),
    ("vxp_fee", "Phí VXP", "CM1", True),
    ("infrastructure_fee", "Phí hạ tầng", "CM1", True),
    ("fulfillment_fee", "Fulfilment", "CM1", True),
    ("packaging_cost", "Đóng gói", "CM1", True),
    ("logistics_order_cost", "Logistics đơn hàng", "CM1", True),
    ("cancel_related_shipping_fee", "Phí vận chuyển/bồi hoàn phát sinh đơn Đã hủy", "CM1", True),
    ("cm1", "CM1", "CM1", False),
    ("ads_product", "Ads sản phẩm", "CM2", True),
    ("ads_live", "Ads live", "CM2", True),
    ("teaser_campaign", "Teaser/campaign live", "CM2", True),
    ("ads_other", "Ads khác/chưa tách", "CM2", True),
    ("affiliate_creator", "Affiliate creator", "CM2_DETAIL", True),
    ("affiliate_partner", "Affiliate partner", "CM2_DETAIL", True),
    ("affiliate_other", "Affiliate khác/chưa tách", "CM2_DETAIL", True),
    ("affiliate_total", "Tổng Affiliate", "CM2_SUBTOTAL", True),
    ("booking_kol_koc", "Booking/KOL/KOC", "CM2", True),
    ("livestream_inhouse", "Livestream in-house", "CM2", True),
    ("content_cost", "Content", "CM2", True),
    ("trade_marketing_cost", "Trade marketing", "CM2", True),
    ("agency_campaign_direct_cost", "Agency/campaign trực tiếp", "CM2", True),
    ("cm2", "CM2", "CM2", False),
    ("backoffice_cost", "Backoffice/chi phí vận hành", "PROFIT", True),
    ("profit", "LỢI NHUẬN", "PROFIT", False),
)

def pnl_lines(current: Mapping[str, Any], previous: Mapping[str, Any]) -> List[Dict[str, Any]]:
    applicable = set(current.get("applicable_cost_codes") or [])
    always = {"gross_sales", "gross_non_cancelled_sales", "sales_deductions_total", "net_sales", "gm1", "cm1", "cm2", "profit"}
    rows = []
    for code, label, tier, is_cost in LINE_DEFINITIONS:
        cur = safe_float(current.get(code)); prev = safe_float(previous.get(code))
        if is_cost and code not in applicable and code not in always and abs(cur) < EPSILON and abs(prev) < EPSILON:
            continue
        if code in {"affiliate_creator", "affiliate_partner", "affiliate_other"} and current.get("channel") == "SHOPEE":
            continue
        role = (
            "subtotal" if tier.endswith("SUBTOTAL") or code in {"net_sales", "gm1", "cm1", "cm2", "profit"}
            else "informational" if tier.endswith("DETAIL") or code == "gross_non_cancelled_sales"
            else "deduction" if tier == "REVENUE_BRIDGE"
            else "cost" if is_cost else "metric"
        )
        rows.append({
            "code": code, "label": label, "tier": tier, "formula_role": role, "is_cost": is_cost,
            "current_amount": cur, "current_pnl_effect": -cur if is_cost else cur,
            "current_pct_of_net_sales": ratio(cur, current.get("net_sales")),
            "previous_amount": prev, "previous_pnl_effect": -prev if is_cost else prev,
            "previous_pct_of_net_sales": ratio(prev, previous.get("net_sales")),
            "delta_amount": cur - prev, "delta_pct": delta_pct(cur, prev),
            "display_policy": "signed_adjustment" if code == "other_sales_adjustment" else ("negative_red_parentheses" if is_cost else "standard"),
            "do_not_double_count": code in {"sales_deductions_total", "affiliate_total"},
        })
    return clean(rows)

def aggregate(rows: Sequence[Mapping[str, Any]], report_date: dt.date) -> Dict[str, Any]:
    additive = [
        "gross_sales_source", "gross_sales", "gross_non_cancelled_sales", "gross_cancelled_sales",
        "seller_discount_source", "seller_voucher_source", "seller_discount", "seller_voucher",
        "other_sales_adjustment",
        "sales_deductions_reported_source", "sales_deductions_reported", "sales_deductions_total",
        "sales_deductions_component_sum", "sales_deductions_reconciliation_gap", "net_sales_source",
        "net_sales", "orders", "success_orders", "cancelled_orders",
        "cancelled_orders_with_gross", "sold_cogs", "gift_cogs", "cogs_total", "cogs_split_reconciliation_gap",
        "gm1", "fixed_fee", "service_fee", "payment_fee", "vxp_fee", "infrastructure_fee",
        "fulfillment_fee", "packaging_cost", "logistics_order_cost", "cancel_related_shipping_fee",
        "cm1_cost_total", "cm1", "ads_product", "ads_live", "teaser_campaign", "ads_other",
        "ads_total", "ads_cost_reported_legacy", "ads_reconciliation_gap", "affiliate_creator",
        "affiliate_partner", "affiliate_other", "affiliate_total", "affiliate_total_source",
        "affiliate_reconciliation_gap", "booking_kol_koc", "livestream_inhouse", "content_cost",
        "trade_marketing_cost", "agency_campaign_direct_cost", "cm2_cost_total", "cm2",
        "backoffice_cost", "profit", "platform_fees_reported_legacy",
        "platform_components_ex_affiliate", "platform_components_plus_affiliate",
        "platform_expected_legacy", "legacy_profit", "payout", "settlement_paid",
        "settlement_unpaid", "estimated_payout",
    ]
    result = {"report_date": report_date.isoformat(), "channel": "TOTAL", "applicable_cost_codes": sorted({code for row in rows for code in row.get("applicable_cost_codes", [])})}
    for field in additive:
        result[field] = sum(safe_float(row.get(field)) for row in rows)
    result["cancellation_rate"] = ratio(result["cancelled_orders"], result["orders"])
    result["gm1_margin"] = ratio(result["gm1"], result["net_sales"])
    result["cm1_margin"] = ratio(result["cm1"], result["net_sales"])
    result["cm2_margin"] = ratio(result["cm2"], result["net_sales"])
    result["profit_margin"] = ratio(result["profit"], result["net_sales"])
    result["platform_reconciliation_gap"] = result["platform_fees_reported_legacy"] - result["platform_components_plus_affiliate"]
    result["profit_vs_legacy_profit"] = result["profit"] - result["legacy_profit"]
    return clean(result)

def rendering_contract(compare_mode: str, compare_date: dt.date) -> Dict[str, Any]:
    return {
        "template_reference": "Bao_cao_CEO_ngay_va_luy_ke_den_13-08-2026_HOAN_CHINH(1).xlsx",
        "preserve_sheets_without_structural_change": list(PRESERVE_SHEETS),
        "daily_pnl_sheets": DAILY_PNL_SHEETS,
        "marketplace_status_rule": {
            "cancelled_status_exact": CANCELLED_STATUS_EXACT,
            "normalization": "Unicode/case-insensitive + trim/collapse whitespace only; exact phrase match after normalization.",
            "returns_refunds": "DISABLED_TEMPORARILY",
            "all_other_statuses": "NORMAL_NON_CANCELLED",
        },
        "pnl_section": {
            "new_title": "P&L NGÀY VÀ SO SÁNH KỲ TRƯỚC", "comparison_mode": compare_mode,
            "comparison_date": compare_date.isoformat(), "source": "daily_pnl_by_channel_map.<CHANNEL>.pnl_lines",
            "gross_rule": "Gross Sales includes every order status. Only exact status 'đã hủy' is classified as cancelled.",
            "bridge_rule": "Net Sales uses total deductions once; detail components explain that subtotal and are not subtracted again.",
            "return_rule": "Return/refund order classification is disabled and no return-order fields are emitted. Every status other than exact Đã hủy is normal.",
            "platform_voucher_rule": "Platform-funded vouchers are ignored and not displayed. Shopee legacy DB column platform_voucher is raw Mã giảm giá của Shop and is emitted only as seller_voucher.",
            "affiliate_rule": "Affiliate is fully shown in CM2. Shopee uses affiliate_total; TikTok shows creator, partner, other and affiliate_total subtotal.",
            "cogs_split_rule": "For NHANH/SHOPEE/TIKTOK show both sold_cogs (Giá vốn hàng bán) and gift_cogs (Giá vốn hàng tặng/khuyến mại). cogs_total must equal sold_cogs + gift_cogs; never subtract cogs_total again after the two detail rows.",
            "hide_non_applicable_cost_lines": True,
        },
        "daily_detail_title": "CHI TIẾT P&L TỪNG NGÀY — GROSS → NET SALES → GM1 → CM1 → CM2 → LỢI NHUẬN",
        "daily_detail_tables": {
            "03_Nhanh": {"source": "mtd_daily_detail_by_channel.NHANH", "columns": ["report_date", "gross_sales", "sales_deductions_total", "net_sales", "orders", "cancellation_rate", "sold_cogs", "gift_cogs", "gm1", "logistics_order_cost", "packaging_cost", "cm1", "ads_total", "cm2", "backoffice_cost", "profit"], "leaf_channel_source": "nhanh_leaf_channel_map", "leaf_daily_source": "nhanh_leaf_daily_detail", "leaf_visibility_field": "show_in_report", "leaf_rule": "Only render Nhanh leaf channels where show_in_report=true. Zero-sales leaf channels remain in JSON as zero-filled data but must not appear in AI analysis, scorecards, legends or narrative."},
            "04_Shopee": {"source": "mtd_daily_detail_by_channel.SHOPEE", "columns": ["report_date", "gross_sales", "gross_non_cancelled_sales", "gross_cancelled_sales", "seller_discount", "seller_voucher", "other_sales_adjustment", "sales_deductions_total", "net_sales", "orders", "cancelled_orders", "cancellation_rate", "sold_cogs", "gift_cogs", "gm1", "fixed_fee", "service_fee", "payment_fee", "cancel_related_shipping_fee", "packaging_cost", "cm1", "ads_total", "affiliate_total", "cm2", "backoffice_cost", "profit"], "no_double_count_rule": "Affiliate is CM2; never subtract legacy aggregate platform_fees again."},
            "05_TikTok": {"source": "mtd_daily_detail_by_channel.TIKTOK", "columns": ["report_date", "gross_sales", "gross_non_cancelled_sales", "gross_cancelled_sales", "seller_discount", "other_sales_adjustment", "sales_deductions_total", "net_sales", "orders", "cancelled_orders", "cancellation_rate", "sold_cogs", "gift_cogs", "gm1", "fixed_fee", "payment_fee", "vxp_fee", "infrastructure_fee", "cancel_related_shipping_fee", "packaging_cost", "cm1", "ads_product", "ads_live", "teaser_campaign", "affiliate_creator", "affiliate_partner", "affiliate_other", "affiliate_total", "booking_kol_koc", "livestream_inhouse", "cm2", "backoffice_cost", "profit"], "no_double_count_rule": "Affiliate total is a subtotal of creator + partner + other; do not subtract both detail and total."},
        },
        "growth_override_rule": "Marketplace Gross/order/cancel metrics in inherited v3.3 blocks are harmonized from the same exact-status direct daily source used by v4.5.",
        "zero_fill_policy": "If a date/channel has no sales row, emit numeric zero instead of null/N/A. This applies to all parent channels and every Nhanh leaf channel.",
        "visibility_policy": {"hide_zero_sales_channels": True, "parent_source": "channel_visibility", "nhanh_leaf_source": "nhanh_leaf_channel_map.<LEAF>.show_in_report", "sales_definition": "A channel is visible when MTD gross_sales or MTD net_sales is non-zero. Costs/orders alone do not make a sales channel visible.", "fixed_sheet_rule": "Keep the six template sheets, but omit zero-sales channels from overview rows, legends, leaf breakdowns and AI narrative."},
        "cogs_split_sources": {
            "NHANH": "nhanh.order_items.is_gift from Nhanh ETL (price=0 is gift)",
            "SHOPEE": "shopee.order_items.is_gift from Shopee ETL gift classifier",
            "TIKTOK": "tiktok.order_items.price=0 => gift; price<>0 => sold; exact Đã hủy => zero COGS",
        },
        "gt_mt_zero_policy": "If GT or MT has no canonical row on a date, render zero without warning.",
        "null_na_policy": "Omit unavailable/non-applicable rows; keep true zero for applicable costs.",
        "percentage_scale": "Ratios are decimals 0..1; apply Excel percentage format.",
    }

def _detail_day(detail_maps: Mapping[str, Mapping[str, Mapping[str, Any]]], channel: str, day: dt.date) -> Dict[str, float]:
    row = detail_maps.get(channel, {}).get(day.isoformat(), {})
    orders = safe_int(row.get("order_count_detail"))
    cancelled = safe_int(row.get("cancelled_order_count_detail"))
    return {
        "gross": safe_float(row.get("gross_all_orders")),
        "orders": orders,
        "cancelled": cancelled,
        "success": max(orders - cancelled, 0),
        "affiliate": safe_float(row.get("affiliate_total")),
        "affiliate_orders": safe_int(row.get("affiliate_order_count_detail")),
    }


def _period_direct(detail_maps: Mapping[str, Mapping[str, Mapping[str, Any]]], channel: str, start: dt.date, end: dt.date) -> Dict[str, float]:
    total = {"gross": 0.0, "orders": 0.0, "cancelled": 0.0, "success": 0.0, "affiliate": 0.0, "affiliate_orders": 0.0}
    for day in date_range(start, end):
        d = _detail_day(detail_maps, channel, day)
        for key in total:
            total[key] += d[key]
    return total


def _set_period_row(row: Dict[str, Any], stats: Mapping[str, float], suffix: str) -> None:
    mapping = {
        f"gross_sales_{suffix}": stats["gross"],
        f"orders_{suffix}": stats["orders"],
        f"success_orders_{suffix}": stats["success"],
        f"cancelled_orders_{suffix}": stats["cancelled"],
        f"cancellation_rate_{suffix}": ratio(stats["cancelled"], stats["orders"]),
    }
    for key, value in mapping.items():
        if key in row:
            row[key] = value


def _recalc_delta(row: Dict[str, Any], current_key: str, previous_key: str, delta_key: str, pct_key: Optional[str] = None) -> None:
    if current_key in row and previous_key in row:
        row[delta_key] = safe_float(row[current_key]) - safe_float(row[previous_key])
        if pct_key and pct_key in row:
            row[pct_key] = delta_pct(row[current_key], row[previous_key])


def harmonize_growth_marketplaces(
    growth: Dict[str, Any],
    detail_maps: Mapping[str, Mapping[str, Mapping[str, Any]]],
    report_date: dt.date,
) -> None:
    """Make inherited v3.3 Gross/order/cancel and affiliate match v4.4 exactly."""
    if not growth:
        return
    week_start = report_date - dt.timedelta(days=report_date.weekday())
    prev_week_start = week_start - dt.timedelta(days=7)
    prev_week_end = prev_week_start + (report_date - week_start)
    mstart = month_start(report_date)
    pstart = month_start(add_months(report_date, -1))
    pend = add_months(report_date, -1)

    periods = {
        "weekly": (week_start, report_date, prev_week_start, prev_week_end, "wtd"),
        "monthly": (mstart, report_date, pstart, pend, "mtd"),
    }

    for prefix, (cur_start, cur_end, prev_start, prev_end, suffix) in periods.items():
        score_key = f"{prefix}_scorecard_by_channel"
        score_rows = growth.get(score_key, [])
        if isinstance(score_rows, list):
            for row in score_rows:
                ch = normalize_channel(row.get("channel"))
                if ch in {"SHOPEE", "TIKTOK"}:
                    _set_period_row(row, _period_direct(detail_maps, ch, cur_start, cur_end), suffix)
            summary = growth.get(f"{prefix}_summary")
            if isinstance(summary, dict) and score_rows:
                for metric in ("gross_sales", "orders", "success_orders", "cancelled_orders"):
                    key = f"{metric}_{suffix}"
                    if key in summary:
                        summary[key] = sum(safe_float(r.get(key)) for r in score_rows)
                rate_key = f"cancellation_rate_{suffix}"
                if rate_key in summary:
                    summary[rate_key] = ratio(summary.get(f"cancelled_orders_{suffix}"), summary.get(f"orders_{suffix}"))

        comp_rows = growth.get(f"{prefix}_comparison_by_channel", [])
        if isinstance(comp_rows, list):
            for row in comp_rows:
                ch = normalize_channel(row.get("channel"))
                if ch in {"SHOPEE", "TIKTOK"}:
                    cur = _period_direct(detail_maps, ch, cur_start, cur_end)
                    prev = _period_direct(detail_maps, ch, prev_start, prev_end)
                    _set_period_row(row, cur, suffix)
                    prev_suffix = f"prev_{suffix}"
                    # v3.3 names previous fields gross_sales_prev_wtd / orders_prev_wtd.
                    for metric, stat_key in (("gross_sales", "gross"), ("orders", "orders"), ("success_orders", "success"), ("cancelled_orders", "cancelled")):
                        key = f"{metric}_prev_{suffix}"
                        if key in row:
                            row[key] = prev[stat_key]
                    prate = f"cancellation_rate_prev_{suffix}"
                    if prate in row:
                        row[prate] = ratio(prev["cancelled"], prev["orders"])
                    _recalc_delta(row, f"gross_sales_{suffix}", f"gross_sales_prev_{suffix}", "gross_sales_delta", "gross_sales_delta_pct")
                    _recalc_delta(row, f"orders_{suffix}", f"orders_prev_{suffix}", "orders_delta", "orders_delta_pct")

            total_comp = growth.get(f"{prefix}_comparison_vs_previous_" + ("week_same_days" if prefix == "weekly" else "month_same_period"))
            if isinstance(total_comp, dict) and comp_rows:
                for metric in ("gross_sales", "orders", "success_orders", "cancelled_orders"):
                    ck = f"{metric}_{suffix}"
                    pk = f"{metric}_prev_{suffix}"
                    if ck in total_comp:
                        total_comp[ck] = sum(safe_float(r.get(ck)) for r in comp_rows)
                    if pk in total_comp:
                        total_comp[pk] = sum(safe_float(r.get(pk)) for r in comp_rows)
                cr = f"cancellation_rate_{suffix}"; pr = f"cancellation_rate_prev_{suffix}"
                if cr in total_comp:
                    total_comp[cr] = ratio(total_comp.get(f"cancelled_orders_{suffix}"), total_comp.get(f"orders_{suffix}"))
                if pr in total_comp:
                    total_comp[pr] = ratio(total_comp.get(f"cancelled_orders_prev_{suffix}"), total_comp.get(f"orders_prev_{suffix}"))
                _recalc_delta(total_comp, f"gross_sales_{suffix}", f"gross_sales_prev_{suffix}", "gross_sales_delta", "gross_sales_delta_pct")
                _recalc_delta(total_comp, f"orders_{suffix}", f"orders_prev_{suffix}", "orders_delta", "orders_delta_pct")

    # Daily trend blocks: exact direct values for marketplaces, recompute TOTAL by date.
    for block_name in ("weekly_daily_trend", "previous_weekly_daily_trend", "monthly_daily_trend", "previous_monthly_daily_trend"):
        rows = growth.get(block_name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            ch = normalize_channel(row.get("channel"))
            if ch in {"SHOPEE", "TIKTOK"} and row.get("trend_date"):
                d = _detail_day(detail_maps, ch, parse_date(str(row["trend_date"])))
                row["gross_sales"] = d["gross"]
                row["orders"] = d["orders"]
                row["success_orders"] = d["success"]
                row["cancelled_orders"] = d["cancelled"]
                if "cancellation_rate" in row:
                    row["cancellation_rate"] = ratio(d["cancelled"], d["orders"])
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        totals: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("trend_date"))
            if normalize_channel(row.get("channel")) == "TOTAL":
                totals[key] = row
            else:
                by_date.setdefault(key, []).append(row)
        for key, total_row in totals.items():
            chrows = by_date.get(key, [])
            for metric in ("gross_sales", "orders", "success_orders", "cancelled_orders"):
                if metric in total_row:
                    total_row[metric] = sum(safe_float(r.get(metric)) for r in chrows)
            if "cancellation_rate" in total_row:
                total_row["cancellation_rate"] = ratio(total_row.get("cancelled_orders"), total_row.get("orders"))

    # Current-vs-previous channel trend blocks.
    for block_name in ("weekly_channel_sales_trend_current_vs_previous", "monthly_channel_sales_trend_current_vs_previous"):
        rows = growth.get(block_name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            ch = normalize_channel(row.get("channel"))
            if ch not in {"SHOPEE", "TIKTOK"}:
                continue
            for side in ("current", "previous"):
                dkey = f"{side}_date"
                if row.get(dkey):
                    d = _detail_day(detail_maps, ch, parse_date(str(row[dkey])))
                    gkey = f"{side}_gross_sales"; okey = f"{side}_orders"
                    if gkey in row: row[gkey] = d["gross"]
                    if okey in row: row[okey] = d["orders"]

    # Recompute total current-vs-previous trends from channel companion rows.
    for prefix in ("weekly", "monthly"):
        channel_rows = growth.get(f"{prefix}_channel_sales_trend_current_vs_previous", [])
        total_rows = growth.get(f"{prefix}_total_sales_trend_current_vs_previous", [])
        if not isinstance(channel_rows, list) or not isinstance(total_rows, list):
            continue
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for row in channel_rows:
            grouped.setdefault(safe_int(row.get("day_no")), []).append(row)
        for row in total_rows:
            peers = grouped.get(safe_int(row.get("day_no")), [])
            for side in ("current", "previous"):
                gkey=f"{side}_gross_sales"; okey=f"{side}_orders"
                if gkey in row: row[gkey]=sum(safe_float(r.get(gkey)) for r in peers)
                if okey in row: row[okey]=sum(safe_float(r.get(okey)) for r in peers)

    # Other gross chart blocks.
    daily_source = {str(r.get("trend_date")): r for r in growth.get("monthly_daily_trend", []) if normalize_channel(r.get("channel")) == "TOTAL"}
    for row in growth.get("monthly_total_sales_trend_chart", []) if isinstance(growth.get("monthly_total_sales_trend_chart"), list) else []:
        src = daily_source.get(str(row.get("trend_date")))
        if src and "gross_sales" in row: row["gross_sales"] = src.get("gross_sales", 0)
    for row in growth.get("monthly_channel_sales_trend_chart", []) if isinstance(growth.get("monthly_channel_sales_trend_chart"), list) else []:
        ch=normalize_channel(row.get("channel"))
        if ch in {"SHOPEE", "TIKTOK"} and row.get("trend_date"):
            row["gross_sales"]=_detail_day(detail_maps,ch,parse_date(str(row["trend_date"]))) ["gross"]
    for row in growth.get("monthly_channel_sales_share_chart", []) if isinstance(growth.get("monthly_channel_sales_share_chart"), list) else []:
        ch=normalize_channel(row.get("channel"))
        if ch in {"SHOPEE", "TIKTOK"} and "gross_sales_mtd" in row:
            row["gross_sales_mtd"]=_period_direct(detail_maps,ch,mstart,report_date)["gross"]

    chart = growth.get("chart_data")
    if isinstance(chart, dict):
        weekly_totals = {str(r.get("trend_date")): r for r in growth.get("weekly_daily_trend", []) if normalize_channel(r.get("channel")) == "TOTAL"}
        for row in chart.get("weekly_net_sales_trend", []) if isinstance(chart.get("weekly_net_sales_trend"), list) else []:
            src=weekly_totals.get(str(row.get("date")))
            if src and "gross_sales" in row: row["gross_sales"]=src.get("gross_sales",0)
        monthly_totals = {str(r.get("trend_date")): r for r in growth.get("monthly_daily_trend", []) if normalize_channel(r.get("channel")) == "TOTAL"}
        for row in chart.get("monthly_net_sales_trend", []) if isinstance(chart.get("monthly_net_sales_trend"), list) else []:
            src=monthly_totals.get(str(row.get("date")))
            if src and "gross_sales" in row: row["gross_sales"]=src.get("gross_sales",0)
        for row in chart.get("monthly_channel_sales_lines", []) if isinstance(chart.get("monthly_channel_sales_lines"), list) else []:
            ch=normalize_channel(row.get("channel"))
            if ch in {"SHOPEE", "TIKTOK"} and row.get("date") and "gross_sales" in row:
                row["gross_sales"]=_detail_day(detail_maps,ch,parse_date(str(row["date"]))) ["gross"]

    # Affiliate amounts/orders: one authoritative direct source for both P&L and growth blocks.
    aff_rows = growth.get("affiliate_cost_by_channel")
    if isinstance(aff_rows, list):
        for row in aff_rows:
            ch=normalize_channel(row.get("channel"))
            if ch in {"SHOPEE", "TIKTOK"}:
                w=_period_direct(detail_maps,ch,week_start,report_date)
                m=_period_direct(detail_maps,ch,mstart,report_date)
                row["affiliate_cost_wtd"]=w["affiliate"]
                row["affiliate_orders_wtd"]=w["affiliate_orders"]
                row["affiliate_cost_mtd"]=m["affiliate"]
                row["affiliate_orders_mtd"]=m["affiliate_orders"]
                row["included_in_current_platform_fees"]=True
                row["counting_note"]="Authoritative amount from the same daily P&L source; already included in legacy platform_fees, do not double count."
        total=next((r for r in aff_rows if normalize_channel(r.get("channel"))=="TOTAL"),None)
        if total:
            members=[r for r in aff_rows if normalize_channel(r.get("channel")) in {"SHOPEE","TIKTOK"}]
            for k in ("affiliate_cost_wtd","affiliate_orders_wtd","affiliate_cost_mtd","affiliate_orders_mtd"):
                total[k]=sum(safe_float(r.get(k)) for r in members)
        if isinstance(chart,dict):
            chart["affiliate_cost_by_channel"]=clean(aff_rows)
            chart["affiliate_cost_summary"]=clean(total or {})

def build_package(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    include_growth_context: bool = True,
) -> Dict[str, Any]:
    report_date = parse_date(report_date_str)
    compare_date = resolve_compare_date(report_date, compare_mode, explicit_compare_date)
    mtd_start = month_start(report_date)
    previous_month_start = month_start(add_months(report_date, -1))
    query_start = min(mtd_start, compare_date, previous_month_start)
    query_end = max(report_date, compare_date)

    script_dir = Path(__file__).resolve().parent
    errors: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []

    growth: Dict[str, Any] = {}
    if include_growth_context:
        try:
            growth = clean(load_growth_builder(script_dir).build_package(report_date_str))
        except Exception as exc:
            errors.append(f"Cannot build base growth package v3.3: {exc}")

    # Normalize the inherited v3.3 affiliate policy to the deployed scorecard:
    # both Shopee and TikTok platform_fees currently include affiliate. The
    # detailed P&L separates affiliate into CM2, so it must not be added again.
    for row in growth.get("affiliate_cost_by_channel", []) if isinstance(growth, dict) else []:
        if normalize_channel(row.get("channel")) in {"SHOPEE", "TIKTOK"}:
            row["included_in_current_platform_fees"] = True
            row["counting_note"] = (
                "Affiliate is already included in legacy platform_fees; show it separately "
                "for analysis but do not add/subtract it a second time."
            )
    if isinstance(growth.get("chart_data"), dict):
        growth["chart_data"]["affiliate_cost_by_channel"] = growth.get("affiliate_cost_by_channel", [])
    if isinstance(growth.get("metric_rules"), dict):
        growth["metric_rules"]["affiliate_rule"] = (
            "Shopee and TikTok affiliate are already included in legacy platform_fees. "
            "Daily P&L separates affiliate into CM2; never double count."
        )

    with connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            base_rows = fetch_scorecard_range(cur, query_start, query_end)
            nhanh_map = lookup(fetch_nhanh_cogs_split(cur, query_start, query_end))
            nhanh_leaf_map = lookup_nhanh_leaf(fetch_nhanh_leaf_detail(cur, query_start, query_end))
            shopee_map = merge_lookups(
                lookup(fetch_shopee_detail(cur, query_start, query_end)),
                lookup(fetch_ads_and_live(cur, "SHOPEE", query_start, query_end)),
            )
            tiktok_map = merge_lookups(
                lookup(fetch_tiktok_detail(cur, query_start, query_end)),
                lookup(fetch_ads_and_live(cur, "TIKTOK", query_start, query_end)),
            )
            status_audit = fetch_marketplace_status_audit(cur, query_start, query_end)

    base_map: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in base_rows:
        channel = normalize_channel(row.get("channel"))
        if channel in EXPECTED_CHANNELS:
            row["channel"] = channel
            base_map[(str(row["report_date"]), channel)] = row

    detail_maps_for_growth = {"SHOPEE": shopee_map, "TIKTOK": tiktok_map}
    harmonize_growth_marketplaces(growth, detail_maps_for_growth, report_date)

    component_maps = {
        "GT": {},
        "MT": {},
        "NHANH": nhanh_map,
        "SHOPEE": shopee_map,
        "TIKTOK": tiktok_map,
    }

    current_rows: Dict[str, Dict[str, Any]] = {}
    previous_rows: Dict[str, Dict[str, Any]] = {}
    mtd_detail: Dict[str, List[Dict[str, Any]]] = {channel: [] for channel in EXPECTED_CHANNELS}

    for channel in EXPECTED_CHANNELS:
        for day in date_range(mtd_start, report_date):
            key = day.isoformat()
            base = base_map.get((key, channel), blank_base(day, channel))
            detail_for_day = component_maps[channel].get(key, {})
            row = compose_pnl(base, detail_for_day, vat_rates)
            canonical_present = (key, channel) in base_map
            detail_signal = any(abs(safe_float(v)) > EPSILON for k, v in detail_for_day.items() if k != "report_date")
            row["canonical_source_row_present"] = canonical_present
            row["zero_filled"] = (not canonical_present) and (not detail_signal)
            prior_month_day = add_months(day, -1)
            prior_base = base_map.get((prior_month_day.isoformat(), channel), blank_base(prior_month_day, channel))
            row["previous_month_same_day"] = prior_month_day.isoformat()
            row["net_sales_previous_month_same_day"] = safe_float(prior_base.get("net_sales"))
            mtd_detail[channel].append(row)

        current_base = base_map.get(
            (report_date.isoformat(), channel), blank_base(report_date, channel)
        )
        previous_base = base_map.get(
            (compare_date.isoformat(), channel), blank_base(compare_date, channel)
        )
        current_rows[channel] = compose_pnl(
            current_base, component_maps[channel].get(report_date.isoformat(), {}), vat_rates
        )
        previous_rows[channel] = compose_pnl(
            previous_base, component_maps[channel].get(compare_date.isoformat(), {}), vat_rates
        )

    by_channel = []
    for channel in EXPECTED_CHANNELS:
        current = current_rows[channel]
        previous = previous_rows[channel]
        by_channel.append({
            "channel": channel,
            "sheet": DAILY_PNL_SHEETS.get(channel),
            "current": current,
            "previous": previous,
            "comparison": {
                "current_date": report_date.isoformat(),
                "previous_date": compare_date.isoformat(),
                "net_sales_delta": safe_float(current["net_sales"]) - safe_float(previous["net_sales"]),
                "net_sales_delta_pct": delta_pct(current["net_sales"], previous["net_sales"]),
                "gm1_delta": safe_float(current["gm1"]) - safe_float(previous["gm1"]),
                "cm1_delta": safe_float(current["cm1"]) - safe_float(previous["cm1"]),
                "cm2_delta": safe_float(current["cm2"]) - safe_float(previous["cm2"]),
                "profit_delta": safe_float(current["profit"]) - safe_float(previous["profit"]),
                "profit_delta_pct": delta_pct(current["profit"], previous["profit"]),
            },
            "pnl_lines": pnl_lines(current, previous),
        })

    # ------------------------------------------------------------
    # Nhanh.vn leaf channels: exact label split + deterministic zero fill.
    # ------------------------------------------------------------
    week_start = report_date - dt.timedelta(days=report_date.weekday())
    leaf_keys = [row["key"] for row in NHANH_LEAF_CATALOG]
    all_leaf_keys = leaf_keys + [NHANH_INTERNAL_UNALLOCATED_KEY]
    nhanh_leaf_daily_detail: Dict[str, List[Dict[str, Any]]] = {key: [] for key in leaf_keys}
    nhanh_leaf_internal_daily: Dict[str, List[Dict[str, Any]]] = {NHANH_INTERNAL_UNALLOCATED_KEY: []}
    nhanh_leaf_channel_map: Dict[str, Dict[str, Any]] = {}

    def build_leaf_row(leaf_key: str, day: dt.date) -> Dict[str, Any]:
        raw = nhanh_leaf_map.get(leaf_key, {}).get(day.isoformat(), {})
        base = nhanh_leaf_base(day, raw)
        row = compose_pnl(base, raw, vat_rates)
        meta = NHANH_KEY_TO_META.get(leaf_key, {
            "key": leaf_key,
            "source_label": "UNALLOCATED",
            "display_name": "Chi phí Nhanh chưa phân bổ nhãn",
        })
        row.update({
            "parent_channel": "NHANH",
            "leaf_channel": leaf_key,
            "source_label": meta["source_label"],
            "display_name": meta["display_name"],
            "source_labels": list(raw.get("source_labels") or []),
            "source_row_present": bool(raw),
            "zero_filled": not bool(raw),
            "internal_non_sales_bucket": leaf_key == NHANH_INTERNAL_UNALLOCATED_KEY,
        })
        return row

    for leaf_key in all_leaf_keys:
        mtd_rows: List[Dict[str, Any]] = []
        for day in date_range(mtd_start, report_date):
            row = build_leaf_row(leaf_key, day)
            prior_day = add_months(day, -1)
            prior_row = build_leaf_row(leaf_key, prior_day)
            row["previous_month_same_day"] = prior_day.isoformat()
            row["net_sales_previous_month_same_day"] = safe_float(prior_row.get("net_sales"))
            mtd_rows.append(row)

        current = build_leaf_row(leaf_key, report_date)
        previous = build_leaf_row(leaf_key, compare_date)
        mtd_summary = aggregate(mtd_rows, report_date)
        wtd_rows = [r for r in mtd_rows if parse_date(str(r["report_date"])) >= week_start]
        wtd_summary = aggregate(wtd_rows, report_date)
        meta = NHANH_KEY_TO_META.get(leaf_key, {
            "key": leaf_key,
            "source_label": "UNALLOCATED",
            "display_name": "Chi phí Nhanh chưa phân bổ nhãn",
        })
        for summary in (mtd_summary, wtd_summary):
            summary.update({
                "channel": "NHANH",
                "parent_channel": "NHANH",
                "leaf_channel": leaf_key,
                "source_label": meta["source_label"],
                "display_name": meta["display_name"],
            })
        show_in_report = leaf_key != NHANH_INTERNAL_UNALLOCATED_KEY and has_sales(mtd_summary)
        nhanh_leaf_channel_map[leaf_key] = {
            "parent_channel": "NHANH",
            "leaf_channel": leaf_key,
            "source_label": meta["source_label"],
            "display_name": meta["display_name"],
            "current": current,
            "previous": previous,
            "mtd": mtd_summary,
            "wtd": wtd_summary,
            "has_sales_current_day": has_sales(current),
            "has_sales_mtd": has_sales(mtd_summary),
            "show_in_report": show_in_report,
            "zero_filled_current_day": bool(current.get("zero_filled")),
            "pnl_lines": pnl_lines(current, previous),
        }
        if leaf_key == NHANH_INTERNAL_UNALLOCATED_KEY:
            nhanh_leaf_internal_daily[leaf_key] = mtd_rows
        else:
            nhanh_leaf_daily_detail[leaf_key] = mtd_rows

    visible_nhanh_leaf_channels = [key for key in leaf_keys if nhanh_leaf_channel_map[key]["show_in_report"]]
    hidden_zero_sales_nhanh_leaf_channels = [key for key in leaf_keys if not nhanh_leaf_channel_map[key]["show_in_report"]]

    # Parent/leaf reconciliation includes the hidden internal cost bucket so no
    # source cost is silently lost. Visible leaf channels remain sales-only.
    nhanh_leaf_reconciliation_daily: List[Dict[str, Any]] = []
    reconcile_fields = (
        "gross_sales", "net_sales", "orders", "success_orders", "cancelled_orders",
        "sold_cogs", "gift_cogs", "cogs_total", "logistics_order_cost", "packaging_cost", "ads_total",
        "backoffice_cost", "profit",
    )
    for idx, day in enumerate(date_range(mtd_start, report_date)):
        parent = mtd_detail["NHANH"][idx]
        leaf_rows_for_day = [
            (nhanh_leaf_daily_detail[k][idx] if k != NHANH_INTERNAL_UNALLOCATED_KEY else nhanh_leaf_internal_daily[k][idx])
            for k in all_leaf_keys
        ]
        gaps: Dict[str, float] = {}
        for field in reconcile_fields:
            leaf_total = sum(safe_float(r.get(field)) for r in leaf_rows_for_day)
            gaps[field] = safe_float(parent.get(field)) - leaf_total
        max_gap = max((abs(v) for v in gaps.values()), default=0.0)
        nhanh_leaf_reconciliation_daily.append({
            "report_date": day.isoformat(),
            "gaps_parent_minus_leaf_total": gaps,
            "max_abs_gap": max_gap,
            "status": "PASS" if max_gap <= EPSILON else "FAIL",
        })
        if max_gap > EPSILON:
            errors.append(f"NHANH leaf reconciliation gap on {day.isoformat()}: max {max_gap:,.0f} VND/count.")

    unallocated_mtd = nhanh_leaf_channel_map[NHANH_INTERNAL_UNALLOCATED_KEY]["mtd"]
    if has_sales(unallocated_mtd):
        errors.append("NHANH has sales on an unmapped label. Fix nhanh.orders.label mapping before using the package.")
    unallocated_ads_mtd = safe_float(unallocated_mtd.get("ads_total"))
    if abs(unallocated_ads_mtd) > EPSILON:
        notes.append(
            f"NHANH has {unallocated_ads_mtd:,.0f} VND MTD ads cost with an unmapped/DEFAULT shop_label. "
            "It stays in a hidden internal bucket, remains included in parent NHANH profit, and is not shown as a sales channel."
        )

    # Parent visibility uses MTD revenue only. A fixed template sheet may remain,
    # but zero-sales parent channels must be omitted from overview/AI narrative.
    channel_visibility: Dict[str, Dict[str, Any]] = {}
    for ch in EXPECTED_CHANNELS:
        mtd_summary = aggregate(mtd_detail[ch], report_date)
        visible = has_sales(mtd_summary)
        channel_visibility[ch] = {
            "gross_sales_mtd": safe_float(mtd_summary.get("gross_sales")),
            "net_sales_mtd": safe_float(mtd_summary.get("net_sales")),
            "show_in_report": visible,
        }
    ai_analysis_channels = [ch for ch in EXPECTED_CHANNELS if channel_visibility[ch]["show_in_report"]]
    hidden_zero_sales_parent_channels = [ch for ch in EXPECTED_CHANNELS if not channel_visibility[ch]["show_in_report"]]

    missing = [
        ch for ch in EXPECTED_CHANNELS
        if (report_date.isoformat(), ch) not in base_map
    ]
    # Owner-approved zero-fill policy v4.5: a missing report-date row is emitted as zero
    # for every parent channel and does not create a missing-ETL warning. Reconciliation
    # checks still catch cases where direct marketplace/leaf data contains material sales.
    zero_filled_channels = list(missing)
    missing_required_channels: List[str] = []

    for ch in ("SHOPEE", "TIKTOK"):
        mismatch = safe_int((status_audit.get(ch) or {}).get("flag_mismatch_count"))
        if mismatch:
            errors.append(
                f"{ch} has {mismatch} DB rows where is_cancelled disagrees with exact status '{CANCELLED_STATUS_EXACT}'. "
                "Patch the ETL and re-import source order files before using the package."
            )

    # v4.5 hard gate: Nhanh/Shopee/TikTok COGS must be fully split into
    # sold goods and gift/promotion goods for every MTD date.
    for ch in ("NHANH", "SHOPEE", "TIKTOK"):
        for row in mtd_detail[ch]:
            day = str(row.get("report_date"))
            if abs(safe_float(row.get("cogs_total"))) > EPSILON and not bool(row.get("cogs_split_available")):
                errors.append(f"{ch} {day}: COGS split source unavailable while cogs_total is non-zero.")
            gap = safe_float(row.get("cogs_split_reconciliation_gap"))
            if abs(gap) > EPSILON:
                errors.append(f"{ch} {day}: COGS split gap {gap:,.0f} VND; sold_cogs + gift_cogs must equal cogs_total.")

    for ch in ("SHOPEE", "TIKTOK"):
        row = current_rows[ch]
        gap = safe_float(row["platform_reconciliation_gap"])
        if abs(gap) > EPSILON:
            warnings.append(f"{ch} platform reconciliation gap: {gap:,.0f} VND.")
        ads_gap = safe_float(row.get("ads_reconciliation_gap"))
        if abs(ads_gap) > EPSILON:
            warnings.append(f"{ch} ads reconciliation gap: {ads_gap:,.0f} VND.")
        affiliate_gap = safe_float(row.get("affiliate_reconciliation_gap"))
        if abs(affiliate_gap) > EPSILON:
            warnings.append(f"{ch} affiliate split reconciliation gap: {affiliate_gap:,.0f} VND.")

    for ch in ("SHOPEE", "TIKTOK"):
        row = current_rows[ch]
        if safe_int(row.get("cancelled_orders")) > 0 and safe_float(row.get("gross_cancelled_sales")) == 0:
            errors.append(f"{ch} has exact-cancelled orders but cancelled Gross Sales is 0; Gross-All-Status source is incomplete.")


    growth_dq = growth.get("data_quality", {}) if growth else {}
    if growth_dq and not bool(growth_dq.get("can_send", True)):
        errors.append("Base growth package v3.3 has can_send=false.")

    status = "FAIL" if errors else ("WARNING" if warnings else "PASS")
    package = dict(growth) if growth else {}
    if "report_rendering_policy" in package:
        package["base_growth_report_rendering_policy"] = package["report_rendering_policy"]

    package["metadata"] = {
        "report_type": PACKAGE_TYPE,
        "report_title": "CEO Daily P&L & Growth Package — Exact Cancel + Nhanh Leaf + Sold/Gift COGS",
        "package_version": PACKAGE_VERSION,
        "package_version_int": PACKAGE_VERSION_INT,
        "base_growth_package_version": growth.get("metadata", {}).get("package_version", "v3.3"),
        "report_date": report_date.isoformat(),
        "comparison_mode": compare_mode,
        "comparison_date": compare_date.isoformat(),
        "current_month_start": mtd_start.isoformat(),
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "scope": "Marketplace exact Đã hủy only; returns disabled; platform-funded voucher excluded; Nhanh split by exact label; zero-fill no-sales rows; zero-sales channels hidden from AI rendering; Sold/Gift COGS split; Gross→Net Sales→GM1→CM1→CM2→Profit.",
    }
    package["data_quality"] = {
        "status": status,
        "can_send": not errors,
        "errors": sorted(set(errors)),
        "warnings": sorted(set(warnings)),
        "notes": sorted(set(notes)),
        "missing_current_scorecard_channels": missing_required_channels,
        "zero_filled_channels": zero_filled_channels,
        "zero_fill_policy": "NO_SALES_OR_NO_ROW_TO_ZERO",
        "hidden_zero_sales_parent_channels": hidden_zero_sales_parent_channels,
        "hidden_zero_sales_nhanh_leaf_channels": hidden_zero_sales_nhanh_leaf_channels,
        "vat_rates": dict(vat_rates),
        "base_growth_data_quality": growth_dq,
        "marketplace_status_rule": {
            "cancelled_status_exact": CANCELLED_STATUS_EXACT,
            "returns_refunds": "DISABLED_TEMPORARILY",
            "all_other_statuses": "NORMAL_NON_CANCELLED",
        },
    }
    package["marketplace_status_audit"] = status_audit
    package["daily_summary"] = {
        "current": aggregate(list(current_rows.values()), report_date),
        "previous": aggregate(list(previous_rows.values()), compare_date),
    }
    package["daily_pnl_by_channel"] = by_channel
    package["daily_pnl_by_channel_map"] = {row["channel"]: row for row in by_channel}
    package["mtd_daily_detail_by_channel"] = mtd_detail
    package["channel_visibility"] = channel_visibility
    package["ai_analysis_channels"] = ai_analysis_channels
    package["nhanh_leaf_channel_catalog"] = [dict(row) for row in NHANH_LEAF_CATALOG]
    package["nhanh_leaf_channel_map"] = nhanh_leaf_channel_map
    package["nhanh_leaf_daily_detail"] = nhanh_leaf_daily_detail
    package["nhanh_leaf_visible_channels_mtd"] = visible_nhanh_leaf_channels
    package["nhanh_leaf_hidden_zero_sales_channels"] = hidden_zero_sales_nhanh_leaf_channels
    package["nhanh_leaf_internal_cost_bucket"] = {
        "key": NHANH_INTERNAL_UNALLOCATED_KEY,
        "show_in_report": False,
        "reason": "Non-sales/unmapped ads labels are kept for reconciliation but never rendered as a sales channel.",
        "summary": nhanh_leaf_channel_map[NHANH_INTERNAL_UNALLOCATED_KEY],
        "daily": nhanh_leaf_internal_daily[NHANH_INTERNAL_UNALLOCATED_KEY],
    }
    package["nhanh_leaf_reconciliation_daily"] = nhanh_leaf_reconciliation_daily
    package["shopee_fee_detail_daily"] = mtd_detail["SHOPEE"]
    package["tiktok_fee_detail_daily"] = mtd_detail["TIKTOK"]
    package["pnl_rules"] = {
        "cancelled_status": "Only normalized exact status 'đã hủy' is cancelled. No substring/fuzzy cancellation matching.",
        "returns_refunds": "Disabled. No return/refund order fields are emitted; every status other than exact 'đã hủy' is processed as normal non-cancelled.",
        "platform_voucher": "Ignored. True platform-funded vouchers are not deducted/displayed. Shopee legacy DB field platform_voucher is mapped as seller_voucher because its raw source is Mã giảm giá của Shop.",
        "gross_sales": "Gross Sales includes all order statuses; gifts remain excluded on Shopee when marked as gifts.",
        "revenue_bridge": "Net Sales = Gross Sales - exact-cancelled sales - seller discount - seller-funded Shop voucher - other sales adjustment. Platform-funded voucher is excluded.",
        "deduction_subtotal": "sales_deductions_total is subtracted once. Detail components explain it and must not be subtracted again.",
        "gm1": "Net Sales - Giá vốn hàng bán - Giá vốn hàng tặng/khuyến mại.",
        "cogs_split": "NHANH uses nhanh.order_items.is_gift; SHOPEE uses shopee.order_items.is_gift; TIKTOK uses exact item price=0 as gift and price<>0 as sold, while exact Đã hủy contributes zero COGS. sold_cogs + gift_cogs must equal cogs_total or package FAILS.",
        "cm1": "GM1 - phí sàn/payment - fulfilment - đóng gói - phí vận chuyển/bồi hoàn phát sinh đơn Đã hủy.",
        "cm2": "CM1 - ads - Affiliate - KOL/KOC - livestream - content - trade marketing - agency/campaign trực tiếp.",
        "profit": "CM2 - backoffice/chi phí vận hành trực tiếp. No EBITDA.",
        "shopee_affiliate": "affiliate_amount is fully shown as affiliate_total in CM2; do not subtract legacy aggregate platform_fees again.",
        "tiktok_affiliate": "commission_amount is split creator/partner/other and affiliate_total is a subtotal only.",
        "gt_mt_zero": "Missing GT/MT daily rows are rendered as zero and do not trigger a warning.",
        "all_channel_zero_fill": "Any parent or Nhanh leaf date with no sales/source row is emitted as numeric zero rather than null/N/A.",
        "nhanh_leaf_split": "NHANH is split by the seven exact nhanh.orders.label values enforced by nhanh_etl.py. Missing leaf dates are zero-filled.",
        "hide_zero_sales": "AI/Excel must not render channels whose MTD gross_sales and net_sales are both zero. Use channel_visibility and nhanh_leaf_channel_map.<leaf>.show_in_report; do not infer visibility independently.",
        "growth_harmonization": "Inherited v3.3 marketplace Gross/order/cancel and affiliate amounts are overwritten from the same v4.5 exact-status daily source.",
        "reconciliation_policy": "Any stale is_cancelled flag or material reconciliation gap is exposed; no hidden balancing cost is created.",
    }
    package["report_rendering_policy"] = rendering_contract(compare_mode, compare_date)
    return clean(package)


def markdown_summary(package: Mapping[str, Any]) -> str:
    meta = package["metadata"]; dq = package["data_quality"]; total = package["daily_summary"]["current"]
    lines = [f"# {meta['report_title']} ({meta['report_date']})", "", f"- Data quality: **{dq['status']}**", f"- Can send: **{dq['can_send']}**", f"- Comparison date: **{meta['comparison_date']}**", "", "## Daily total", f"- Gross Sales all statuses: **{safe_float(total.get('gross_sales')):,.0f}**", f"- Cancelled Gross Sales: **{safe_float(total.get('gross_cancelled_sales')):,.0f}**", f"- Net Sales: **{safe_float(total.get('net_sales')):,.0f}**", f"- COGS hàng bán: **{safe_float(total.get('sold_cogs')):,.0f}**", f"- COGS hàng tặng: **{safe_float(total.get('gift_cogs')):,.0f}**", f"- GM1: **{safe_float(total.get('gm1')):,.0f}**", f"- CM1: **{safe_float(total.get('cm1')):,.0f}**", f"- CM2: **{safe_float(total.get('cm2')):,.0f}**", f"- Profit: **{safe_float(total.get('profit')):,.0f}**", "", "> Dùng file JSON đầy đủ để tạo Excel."]
    visible_leaf = package.get("nhanh_leaf_visible_channels_mtd", [])
    if visible_leaf:
        lines += ["", "## Nhanh leaf channels shown to AI"] + [f"- {x}" for x in visible_leaf]
    if dq["warnings"]: lines += ["", "## Warnings"] + [f"- {x}" for x in dq["warnings"]]
    if dq["errors"]: lines += ["", "## Errors"] + [f"- {x}" for x in dq["errors"]]
    return "\n".join(lines) + "\n"

def store_best_effort(
    report_date: str,
    package: Mapping[str, Any],
    markdown: str,
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
                WHERE table_schema='control_tower' AND table_name='report_packages'
                """
            )
            colmap = {r["column_name"]: r for r in cur.fetchall()}
            values: Dict[str, Any] = {}

            def add(names: Iterable[str], value: Any) -> None:
                for name in names:
                    if name in colmap and name not in values:
                        values[name] = value
                        return

            add(("report_date", "package_date", "date"), report_date)
            add(("report_type", "package_type", "type"), PACKAGE_TYPE)
            for name in ("version", "package_version", "schema_version", "package_schema_version"):
                if name in colmap:
                    dtype = str(colmap[name]["data_type"]).lower()
                    udt = str(colmap[name]["udt_name"]).lower()
                    values[name] = PACKAGE_VERSION_INT if ("int" in dtype or udt.startswith("int")) else PACKAGE_VERSION
                    break

            payload = json.dumps(package, ensure_ascii=False, default=json_default)
            add(("payload", "package_json", "package_data", "data", "json_payload"), payload)
            add(("markdown", "markdown_summary", "summary_md"), markdown)
            add(("json_path", "package_path", "file_path"), json_path)
            add(("md_path", "markdown_path"), md_path)
            add(("checksum", "package_checksum", "sha256"), hashlib.sha256(payload.encode()).hexdigest())
            add(("created_at", "generated_at"), dt.datetime.now())
            add(("updated_at",), dt.datetime.now())

            columns = list(values)
            if not columns:
                print("WARNING: no compatible report_packages columns; skip --store-db")
                return
            sql = (
                f"INSERT INTO control_tower.report_packages ({', '.join(columns)}) "
                f"VALUES ({', '.join(['%s'] * len(columns))})"
            )
            try:
                cur.execute(sql, [values[c] for c in columns])
                conn.commit()
                print("Stored package into control_tower.report_packages")
            except Exception as exc:
                conn.rollback()
                print(f"WARNING: could not store package: {exc}")


def self_test() -> int:
    failures: List[str] = []

    # Shopee: seller_voucher is seller-funded; platform-funded voucher never appears.
    shopee_base = {"report_date":"2026-08-13","channel":"SHOPEE","gross_sales":120,"net_sales":100,"cogs":30,"platform_fees_reported":22,"shipping_return_fee":1,"ads_cost":10,"backoffice_fee":5,"packaging_cost":2,"legacy_profit":30,"orders":10,"success_orders":8,"cancelled_orders":2}
    shopee_detail = {"gross_all_orders":150,"gross_non_cancelled_orders":130,"gross_cancelled_orders":20,"order_count_detail":10,"cancelled_order_count_detail":2,"seller_discount":5,"seller_voucher":3,"sold_cogs":28,"gift_cogs":2,"fixed_fee":10,"service_fee":5,"payment_fee":2,"affiliate_total":5,"cancel_related_shipping_fee":1}
    shopee = compose_pnl(shopee_base, shopee_detail, {})
    for key, expected in {"gross_sales":150,"gross_non_cancelled_sales":130,"gross_cancelled_sales":20,"sales_deductions_total":50,"gm1":70,"cm1":50,"cm2":35,"profit":30,"affiliate_total":5,"orders":10,"cancelled_orders":2,"seller_voucher":3,"sold_cogs":28,"gift_cogs":2,"cogs_split_reconciliation_gap":0}.items():
        if abs(safe_float(shopee[key])-expected)>1e-9: failures.append(f"Shopee {key}: expected {expected}, got {shopee[key]}")
    if abs(safe_float(shopee["other_sales_adjustment"])-22)>1e-9: failures.append("Shopee other_sales_adjustment should be 22")
    codes={r["code"] for r in pnl_lines(shopee,shopee)}
    if "affiliate_total" not in codes: failures.append("Shopee should show affiliate_total")
    if not {"sold_cogs","gift_cogs"}.issubset(codes): failures.append("Shopee should show sold_cogs and gift_cogs")
    if "seller_voucher" not in codes: failures.append("Shopee should show seller-funded Shop voucher")
    if any(k in shopee for k in ("gross_returned_sales","returned_orders","return_rate","platform_voucher")): failures.append("Shopee must not emit return fields or platform_voucher")
    if "ebitda" in codes: failures.append("EBITDA must not be present")

    tiktok_base = {"report_date":"2026-08-13","channel":"TIKTOK","gross_sales":130,"net_sales":90,"cogs":30,"platform_fees_reported":22,"ads_cost":10,"backoffice_fee":5,"packaging_cost":2,"legacy_profit":20,"orders":10,"success_orders":9,"cancelled_orders":1}
    tiktok_detail = {"gross_all_orders":130,"gross_non_cancelled_orders":118,"gross_cancelled_orders":12,"order_count_detail":10,"cancelled_order_count_detail":1,"seller_discount":10,"seller_voucher":0,"sold_cogs":25,"gift_cogs":5,"fixed_fee":8,"payment_fee":3,"vxp_fee":4,"infrastructure_fee":2,"affiliate_total":5,"affiliate_creator":3,"affiliate_partner":2,"cancel_related_shipping_fee":1,"ads_product":7,"ads_live":3}
    tiktok=compose_pnl(tiktok_base,tiktok_detail,{})
    for key, expected in {"gross_sales":130,"gross_non_cancelled_sales":118,"gross_cancelled_sales":12,"net_sales":90,"gm1":60,"cm1":40,"cm2":25,"profit":20,"affiliate_creator":3,"affiliate_partner":2,"affiliate_total":5,"orders":10,"cancelled_orders":1,"sold_cogs":25,"gift_cogs":5,"cogs_split_reconciliation_gap":0}.items():
        if abs(safe_float(tiktok[key])-expected)>1e-9: failures.append(f"TikTok {key}: expected {expected}, got {tiktok[key]}")
    codes={r["code"] for r in pnl_lines(tiktok,tiktok)}
    if not {"affiliate_creator","affiliate_partner","affiliate_total"}.issubset(codes): failures.append("TikTok should show full affiliate detail and total")
    if not {"sold_cogs","gift_cogs"}.issubset(codes): failures.append("TikTok should show sold_cogs and gift_cogs")
    if any(k in tiktok for k in ("gross_returned_sales","returned_orders","return_rate","platform_voucher")): failures.append("TikTok must not emit return fields or platform_voucher")
    if sql_exact_cancelled("status") != "lower(regexp_replace(btrim(COALESCE(status,'')), '[[:space:]]+', ' ', 'g')) = 'đã hủy'": failures.append("Exact cancel SQL predicate changed unexpectedly")
    if has_sales({"gross_sales":0,"net_sales":0,"orders":9,"ads_total":100}): failures.append("Zero-sales channel must stay hidden even when it has costs/orders")
    if not has_sales({"gross_sales":2,"net_sales":0}): failures.append("Positive Gross must make a channel visible")
    if len(NHANH_LEAF_CATALOG) != 7 or len(NHANH_LABEL_TO_KEY) != 7: failures.append("Nhanh leaf catalog must contain exactly seven exact labels")
    if failures:
        print("SELF-TEST FAILED",file=sys.stderr)
        for item in failures: print(f"- {item}",file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print(json.dumps({"status_rule":CANCELLED_STATUS_EXACT,"shopee":shopee,"tiktok":tiktok},ensure_ascii=False,indent=2))
    return 0

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Report date YYYY-MM-DD")
    parser.add_argument(
        "--compare-mode",
        choices=("previous_day", "previous_week_same_day", "previous_month_same_day"),
        default="previous_day",
    )
    parser.add_argument("--compare-date", default=None)
    parser.add_argument(
        "--vat-rates",
        default=os.getenv("DAILY_PNL_VAT_RATES", ""),
        help='JSON object, e.g. {"NHANH":0.08,"SHOPEE":0.08,"TIKTOK":0.08}',
    )
    parser.add_argument("--require-vat-rates", action="store_true")
    parser.add_argument("--without-growth-context", action="store_true")
    parser.add_argument("--out-dir", default="outbox/packages")
    parser.add_argument("--store-db", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        return self_test()
    if not args.date:
        parser.error("--date is required unless --self-test is used")

    try:
        report_date = parse_date(args.date).isoformat()
        if args.compare_date:
            parse_date(args.compare_date)
        vat_rates = parse_vat_rates(args.vat_rates)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.require_vat_rates:
        missing = [ch for ch in DAILY_PNL_SHEETS if ch not in vat_rates]
        if missing:
            print("ERROR: missing VAT rates for " + ", ".join(missing), file=sys.stderr)
            return 2

    package = build_package(
        report_date,
        args.compare_mode,
        args.compare_date,
        vat_rates,
        include_growth_context=not args.without_growth_context,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ceo_daily_pnl_package_v4_5_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_5_{report_date}.md"
    json_path.write_text(
        json.dumps(package, ensure_ascii=False, indent=2, default=json_default),
        encoding="utf-8",
    )
    md = markdown_summary(package)
    md_path.write_text(md, encoding="utf-8")
    if args.store_db:
        store_best_effort(report_date, package, md, str(json_path), str(md_path))

    dq = package["data_quality"]
    print(f"JSON package: {json_path}")
    print(f"Markdown summary: {md_path}")
    print(f"Data quality: {dq['status']}")
    if dq["warnings"]:
        print("Warnings:")
        for warning in dq["warnings"]:
            print(f"- {warning}")
    if dq["errors"]:
        print("Errors:")
        for error in dq["errors"]:
            print(f"- {error}")
    return 0 if dq["can_send"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
