#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.6.1.

v4.6.1 is a cleanup/guard release layered on v4.6.  It intentionally keeps
the v4.6 financial waterfall while tightening six contracts:

1. Nhanh status matching uses harmless normalization (Unicode NFC,
   case-insensitive, trim/collapse whitespace) followed by exact phrase match.
   This classifies e.g. "Hệ Thống hủy" and "Hệ thống hủy" identically without
   accent folding or substring/fuzzy matching.
2. Nhanh cancel-like diagnostics use whole tokens, so "Đang chuyển" is not a
   false positive.  Any genuinely unmapped cancel-like status becomes a hard
   data-quality error requiring business review.
3. No-sales trend/chart points and zero-filled daily rate KPIs are numeric 0,
   not null, while true N/A fields remain null.
4. All rendering/P&L metadata is rewritten to the current channel-specific
   cancellation contract (TikTok exact Canceled, not Đã hủy) and v4.6.1.
5. AI/Excel display semantics explicitly forbid labeling Nhanh success_orders
   as literal "Đơn thành công".  success_orders is the non-cancelled
   compatibility field; successful_orders_exact is literal status Thành công.
6. Nhanh week/month growth is fully harmonized from the same direct daily P&L
   (including Net, COGS, GM, shipping, Backoffice, Ads, packaging and Profit),
   then TOTAL/comparison/forecast/chart blocks are recomputed.

The package still ends at Profit/LỢI NHUẬN. No EBITDA is introduced.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PACKAGE_VERSION = "v4.6.1"
PACKAGE_VERSION_INT = 461
BASE_FILENAME = "build_ceo_daily_pnl_package_v4_6.py"
EPSILON = 1.0

SHOPEE_CANCELLED_STATUS_EXACT = "đã hủy"
TIKTOK_CANCELLED_STATUS_EXACT = "canceled"
NHANH_CANCELLED_STATUSES_EXACT: Tuple[str, ...] = (
    "Đã hủy",
    "Hệ Thống hủy",
    "Khách hủy",
    "HVC hủy",
)
NHANH_RETURN_STATUSES_EXACT: Tuple[str, ...] = (
    "Đang hoàn",
    "Xác nhận hoàn",
    "Đã hoàn",
)
NHANH_RETURN_IN_PROGRESS_STATUSES_EXACT: Tuple[str, ...] = (
    "Đang hoàn",
    "Xác nhận hoàn",
)
NHANH_FAILED_STATUSES_EXACT: Tuple[str, ...] = ("Thất bại",)
NHANH_SUCCESS_STATUS_EXACT = "Thành công"
NHANH_NO_BO_STATUSES_EXACT: Tuple[str, ...] = tuple(
    dict.fromkeys((*NHANH_CANCELLED_STATUSES_EXACT, "Đã hoàn"))
)

# The base v4.6 audit callback only receives start/end.  v4.6.1 stores the exact
# reporting periods here before calling the base builder so audits do not gate
# irrelevant dates between previous-MTD and current-MTD.
_AUDIT_PERIODS: List[Tuple[dt.date, dt.date, str]] = []
_ACTIVE_VAT_RATES: Dict[str, float] = {}


def _load_v46():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. v4.6.1 is intentionally layered on the deployed v4.6 builder."
        )
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v46_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_status(value: Any) -> str:
    """Harmless status normalization only; no accent folding."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_normalized(status_expr: str) -> str:
    # PostgreSQL lower + trim/collapse whitespace mirrors the business-visible
    # normalization needed for Vietnamese status capitalization.  No unaccent.
    return (
        "lower(regexp_replace(btrim(COALESCE(" + status_expr + ",'')), "
        "'[[:space:]]+', ' ', 'g'))"
    )


def _sql_status_in(status_expr: str, values: Sequence[str]) -> str:
    targets = ", ".join(_sql_literal(_canonical_status(v)) for v in values)
    return f"{_sql_normalized(status_expr)} IN ({targets})"


def _sql_status_eq(status_expr: str, value: str) -> str:
    return f"{_sql_normalized(status_expr)} = {_sql_literal(_canonical_status(value))}"


def sql_nhanh_cancelled(status_expr: str) -> str:
    return _sql_status_in(status_expr, NHANH_CANCELLED_STATUSES_EXACT)


def sql_nhanh_return(status_expr: str) -> str:
    return _sql_status_in(status_expr, NHANH_RETURN_STATUSES_EXACT)


def sql_nhanh_return_in_progress(status_expr: str) -> str:
    return _sql_status_in(status_expr, NHANH_RETURN_IN_PROGRESS_STATUSES_EXACT)


def sql_nhanh_failed(status_expr: str) -> str:
    return _sql_status_in(status_expr, NHANH_FAILED_STATUSES_EXACT)


def sql_nhanh_no_bo(status_expr: str) -> str:
    return _sql_status_in(status_expr, NHANH_NO_BO_STATUSES_EXACT)


def _status_tokens(value: Any) -> set[str]:
    text = _canonical_status(value)
    return set(re.findall(r"\w+", text, flags=re.UNICODE))


def is_cancel_like_status_text(value: Any) -> bool:
    """Diagnostic only: match cancel words as whole tokens, never substrings."""
    return bool(_status_tokens(value) & {"hủy", "huỷ", "huy", "cancel", "canceled", "cancelled"})


def _periods_for_report(v46, report_date: dt.date, compare_date: dt.date) -> List[Tuple[dt.date, dt.date, str]]:
    mstart = v46._load_base().month_start(report_date) if False else report_date.replace(day=1)
    # Avoid importing v4.5 here; the calendar helper is simple and deterministic.
    prev_same_day = _add_months(report_date, -1)
    prev_mstart = prev_same_day.replace(day=1)
    week_start = report_date - dt.timedelta(days=report_date.weekday())
    prev_week_start = week_start - dt.timedelta(days=7)
    prev_week_end = prev_week_start + (report_date - week_start)
    raw = [
        (mstart, report_date, "current_mtd"),
        (prev_mstart, prev_same_day, "previous_mtd_same_period"),
        (prev_week_start, prev_week_end, "previous_wtd_same_days"),
        (compare_date, compare_date, "comparison_date"),
    ]
    # Merge only identical ranges; keeping labels helps audit traceability.
    seen: set[Tuple[dt.date, dt.date]] = set()
    out: List[Tuple[dt.date, dt.date, str]] = []
    for start, end, label in raw:
        key = (start, end)
        if key not in seen:
            seen.add(key)
            out.append((start, end, label))
    return out


def _add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    next_month = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    last_day = (next_month - dt.timedelta(days=1)).day
    return dt.date(year, month, min(value.day, last_day))


def _audit_scope_sql(date_expr: str, fallback_start: dt.date, fallback_end: dt.date) -> Tuple[str, Tuple[Any, ...]]:
    periods = _AUDIT_PERIODS or [(fallback_start, fallback_end, "fallback")]
    clauses: List[str] = []
    params: List[Any] = []
    for start, end, _label in periods:
        clauses.append(f"({date_expr} BETWEEN %s AND %s)")
        params.extend((start, end))
    return "(" + " OR ".join(clauses) + ")", tuple(params)


def _fetch_marketplace_status_audit_v461(v46, base, cur, start: dt.date, end: dt.date) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    specs = {
        "SHOPEE": ("shopee", "orders", "status", SHOPEE_CANCELLED_STATUS_EXACT),
        "TIKTOK": ("tiktok", "orders_pnl", "order_status", TIKTOK_CANCELLED_STATUS_EXACT),
    }
    scope_sql, scope_params = _audit_scope_sql("o.order_date", start, end)
    for channel, (schema, table, status_col, target) in specs.items():
        if not base.relation_exists(cur, schema, table):
            result[channel] = {"available": False, "status_rows": [], "flag_mismatch_count": 0}
            continue
        pred = v46.sql_marketplace_cancelled(f"o.{status_col}")
        rows = base.fetch_all(
            cur,
            f"""
            SELECT
                COALESCE(NULLIF(btrim(o.{status_col}),''), '<BLANK>') AS raw_status,
                ({pred}) AS classified_cancelled,
                COALESCE(o.is_cancelled,false) AS stored_is_cancelled,
                COUNT(*)::numeric AS orders
            FROM {schema}.{table} o
            WHERE {scope_sql}
            GROUP BY 1,2,3
            ORDER BY classified_cancelled DESC, orders DESC, raw_status
            """,
            scope_params,
        )
        mismatch = base.fetch_all(
            cur,
            f"""
            SELECT COUNT(*)::numeric AS mismatch_count
            FROM {schema}.{table} o
            WHERE {scope_sql}
              AND COALESCE(o.is_cancelled,false) <> ({pred})
            """,
            scope_params,
        )
        result[channel] = {
            "available": True,
            "rule": f"normalized exact raw status == {('Đã hủy' if channel == 'SHOPEE' else 'Canceled')!r}",
            "cancelled_status_exact": "Đã hủy" if channel == "SHOPEE" else "Canceled",
            "return_refund_classification": "DISABLED_AS_CANCELLED",
            "audit_scope": [
                {"label": label, "start": a.isoformat(), "end": b.isoformat()}
                for a, b, label in (_AUDIT_PERIODS or [(start, end, "fallback")])
            ],
            "status_rows": rows,
            "flag_mismatch_count": base.safe_int(mismatch[0].get("mismatch_count")) if mismatch else 0,
        }
    return base.clean(result)


def _fetch_nhanh_leaf_detail_v461(v46, base, cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Direct Nhanh P&L by label using normalized exact status semantics."""
    if not base.relation_exists(cur, "nhanh", "orders"):
        return []

    order_cols = base.relation_columns(cur, "nhanh", "orders")
    if not {"order_date", "label", "status", "order_value"}.issubset(order_cols):
        raise RuntimeError("nhanh.orders is missing one of order_date/label/status/order_value")

    item_exists = base.relation_exists(cur, "nhanh", "order_items")
    item_cols = base.relation_columns(cur, "nhanh", "order_items") if item_exists else set()
    gift_expr = "COALESCE(i.is_gift,false)" if "is_gift" in item_cols else "false"

    cancel = sql_nhanh_cancelled("o.status")
    ret = sql_nhanh_return("o.status")
    ret_progress = sql_nhanh_return_in_progress("o.status")
    failed = sql_nhanh_failed("o.status")
    no_bo = sql_nhanh_no_bo("o.status")
    success_exact = _sql_status_eq("o.status", NHANH_SUCCESS_STATUS_EXACT)
    completed_return = _sql_status_eq("o.status", "Đã hoàn")

    if item_exists:
        item_cte = f"""
        item AS (
            SELECT
                i.order_id,
                SUM(COALESCE(i.quantity,0) * COALESCE(i.price,0))::numeric AS gross_sales,
                SUM(CASE WHEN NOT ({gift_expr})
                         THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS sold_cogs,
                SUM(CASE WHEN ({gift_expr})
                         THEN COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0) ELSE 0 END)::numeric AS gift_cogs,
                SUM(COALESCE(i.quantity,0) * COALESCE(i.unit_cogs,0))::numeric AS cogs_total,
                SUM(COALESCE(i.quantity,0))::numeric AS item_qty
            FROM nhanh.order_items i
            GROUP BY i.order_id
        )
        """
        item_join = "LEFT JOIN item i ON i.order_id=o.id"
        igross = "COALESCE(i.gross_sales,0)"
        isold = "COALESCE(i.sold_cogs,0)"
        igift = "COALESCE(i.gift_cogs,0)"
        icogs = "COALESCE(i.cogs_total,0)"
        iqty = "COALESCE(i.item_qty,0)"
    else:
        item_cte = "item AS (SELECT NULL::bigint AS order_id WHERE false)"
        item_join = ""
        igross = isold = igift = icogs = iqty = "0::numeric"

    shipping_expr = "COALESCE(o.shipping_fee,0)" if "shipping_fee" in order_cols else "0::numeric"

    sales_rows = base.fetch_all(
        cur,
        f"""
        WITH {item_cte}
        SELECT
            o.order_date::date AS report_date,
            COALESCE(NULLIF(btrim(o.label),''), 'UNALLOCATED')::text AS source_label,
            SUM({igross})::numeric AS gross_sales,
            SUM(CASE WHEN NOT ({cancel}) THEN {igross} ELSE 0 END)::numeric AS gross_non_cancelled_orders,
            SUM(CASE WHEN ({cancel}) THEN {igross} ELSE 0 END)::numeric AS gross_cancelled_orders,
            SUM(COALESCE(o.order_value,0))::numeric AS net_sales,
            SUM({isold})::numeric AS sold_cogs,
            SUM({igift})::numeric AS gift_cogs,
            SUM({icogs})::numeric AS cogs_total,
            SUM({shipping_expr})::numeric AS shipping_return_fee,
            SUM(CASE WHEN ({no_bo}) OR COALESCE(o.order_value,0) <= 0
                     THEN 0 ELSE COALESCE(o.order_value,0) * 0.15 END)::numeric AS backoffice_fee,
            SUM({iqty} * 2000)::numeric AS packaging_cost,
            COUNT(*)::numeric AS orders,
            COUNT(*) FILTER (WHERE NOT ({cancel}))::numeric AS success_orders,
            COUNT(*) FILTER (WHERE ({cancel}))::numeric AS cancelled_orders,
            COUNT(*)::numeric AS order_count_detail,
            COUNT(*) FILTER (WHERE NOT ({cancel}))::numeric AS non_cancelled_order_count_detail,
            COUNT(*) FILTER (WHERE ({cancel}))::numeric AS cancelled_order_count_detail,
            COUNT(*) FILTER (WHERE ({cancel}) AND ABS({igross}) > 0)::numeric AS cancelled_orders_with_gross,
            COUNT(*) FILTER (WHERE ({success_exact}))::numeric AS successful_orders_exact,
            COUNT(*) FILTER (WHERE ({ret}))::numeric AS returned_orders_status,
            COUNT(*) FILTER (WHERE ({ret_progress}))::numeric AS return_in_progress_orders,
            COUNT(*) FILTER (WHERE ({completed_return}))::numeric AS completed_return_orders,
            COUNT(*) FILTER (WHERE ({failed}))::numeric AS failed_orders_status
        FROM nhanh.orders o
        {item_join}
        WHERE o.order_date BETWEEN %s AND %s
        GROUP BY o.order_date::date, COALESCE(NULLIF(btrim(o.label),''), 'UNALLOCATED')
        ORDER BY report_date, source_label
        """,
        (start, end),
    )

    ads_rows: List[Dict[str, Any]] = []
    if base.relation_exists(cur, "nhanh", "ads_costs"):
        ads_cols = base.relation_columns(cur, "nhanh", "ads_costs")
        if "cost_vnd_vat" in ads_cols:
            ads_expr = "COALESCE(a.cost_vnd_vat,0)"
        elif "cost_vnd" in ads_cols:
            ads_expr = "COALESCE(a.cost_vnd,0)"
        else:
            ads_expr = "0::numeric"
        label_col = "shop_label" if "shop_label" in ads_cols else ("label" if "label" in ads_cols else None)
        if label_col:
            ads_rows = base.fetch_all(
                cur,
                f"""
                SELECT
                    a.ads_date::date AS report_date,
                    COALESCE(NULLIF(btrim(a.{label_col}),''), 'UNALLOCATED')::text AS source_label,
                    SUM({ads_expr})::numeric AS ads_cost
                FROM nhanh.ads_costs a
                WHERE a.ads_date BETWEEN %s AND %s
                GROUP BY a.ads_date::date, COALESCE(NULLIF(btrim(a.{label_col}),''), 'UNALLOCATED')
                ORDER BY report_date, source_label
                """,
                (start, end),
            )

    numeric_fields = (
        "gross_sales", "gross_non_cancelled_orders", "gross_cancelled_orders", "net_sales",
        "sold_cogs", "gift_cogs", "cogs_total", "shipping_return_fee", "backoffice_fee",
        "packaging_cost", "orders", "success_orders", "cancelled_orders", "order_count_detail",
        "non_cancelled_order_count_detail", "cancelled_order_count_detail", "cancelled_orders_with_gross",
        "successful_orders_exact", "returned_orders_status", "return_in_progress_orders",
        "completed_return_orders", "failed_orders_status", "ads_cost",
    )
    buckets: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def map_leaf(source_label: str) -> str:
        if source_label in base.NHANH_LABEL_TO_KEY:
            return base.NHANH_LABEL_TO_KEY[source_label]
        return base.NHANH_INTERNAL_UNALLOCATED_KEY

    def ensure(day: str, leaf_key: str) -> Dict[str, Any]:
        key = (day, leaf_key)
        if key not in buckets:
            buckets[key] = {"report_date": day, "leaf_channel": leaf_key, "source_labels": []}
            for field in numeric_fields:
                buckets[key][field] = 0.0
        return buckets[key]

    for raw in sales_rows:
        day = str(raw.get("report_date"))
        label = str(raw.get("source_label") or "UNALLOCATED")
        leaf = map_leaf(label)
        row = ensure(day, leaf)
        if label not in row["source_labels"]:
            row["source_labels"].append(label)
        for field in numeric_fields:
            if field != "ads_cost":
                row[field] += base.safe_float(raw.get(field))

    for raw in ads_rows:
        day = str(raw.get("report_date"))
        label = str(raw.get("source_label") or "UNALLOCATED")
        leaf = map_leaf(label)
        row = ensure(day, leaf)
        if label not in row["source_labels"]:
            row["source_labels"].append(label)
        row["ads_cost"] += base.safe_float(raw.get("ads_cost"))

    out: List[Dict[str, Any]] = []
    for row in buckets.values():
        row["gross_all_orders"] = row["gross_sales"]
        row["ads_other"] = row["ads_cost"]
        row["backoffice_direct"] = row["backoffice_fee"]
        row["packaging_calculated"] = row["packaging_cost"]
        row["logistics_order_cost_direct"] = row["shipping_return_fee"]
        row["cogs_split_available"] = True
        out.append(base.clean(row))
    return sorted(out, key=lambda r: (str(r.get("report_date")), str(r.get("leaf_channel"))))


def _fetch_nhanh_status_audit_v461(v46, base, cur, start: dt.date, end: dt.date) -> Dict[str, Any]:
    if not base.relation_exists(cur, "nhanh", "orders"):
        return {"available": False, "status_rows": [], "db_flag_mismatch_count": 0}
    cancel = sql_nhanh_cancelled("o.status")
    ret = sql_nhanh_return("o.status")
    failed = sql_nhanh_failed("o.status")
    scope_sql, scope_params = _audit_scope_sql("o.order_date", start, end)
    rows = base.fetch_all(
        cur,
        f"""
        SELECT
            COALESCE(NULLIF(btrim(o.status),''), '<BLANK>') AS raw_status,
            ({cancel}) AS classified_cancelled,
            ({ret}) AS classified_return_status,
            ({failed}) AS classified_failed_status,
            COALESCE(o.is_cancelled,false) AS stored_is_cancelled,
            COUNT(*)::numeric AS orders
        FROM nhanh.orders o
        WHERE {scope_sql}
        GROUP BY 1,2,3,4,5
        ORDER BY orders DESC, raw_status
        """,
        scope_params,
    )
    mismatch = base.fetch_all(
        cur,
        f"""
        SELECT
            COUNT(*)::numeric AS mismatch_count,
            COUNT(*) FILTER (WHERE ({cancel}) AND NOT COALESCE(o.is_cancelled,false))::numeric AS false_negative_cancel_count,
            COUNT(*) FILTER (WHERE NOT ({cancel}) AND COALESCE(o.is_cancelled,false))::numeric AS false_positive_cancel_count
        FROM nhanh.orders o
        WHERE {scope_sql}
          AND COALESCE(o.is_cancelled,false) <> ({cancel})
        """,
        scope_params,
    )
    stats = mismatch[0] if mismatch else {}
    cancel_like = [
        r for r in rows
        if is_cancel_like_status_text(r.get("raw_status")) and not bool(r.get("classified_cancelled"))
    ]
    return base.clean({
        "available": True,
        "cancelled_statuses_exact": list(NHANH_CANCELLED_STATUSES_EXACT),
        "return_statuses_exact_not_cancelled": list(NHANH_RETURN_STATUSES_EXACT),
        "failed_statuses_exact_not_cancelled": list(NHANH_FAILED_STATUSES_EXACT),
        "successful_status_exact_informational": NHANH_SUCCESS_STATUS_EXACT,
        "normalization": "Unicode NFC + case-insensitive + trim/collapse whitespace; exact normalized phrase only; no accent folding/substrings",
        "report_uses_raw_status_direct": True,
        "audit_scope": [
            {"label": label, "start": a.isoformat(), "end": b.isoformat()}
            for a, b, label in (_AUDIT_PERIODS or [(start, end, "fallback")])
        ],
        "status_rows": rows,
        "db_flag_mismatch_count": base.safe_int(stats.get("mismatch_count")),
        "db_false_negative_cancel_count": base.safe_int(stats.get("false_negative_cancel_count")),
        "db_false_positive_cancel_count": base.safe_int(stats.get("false_positive_cancel_count")),
        "unmapped_cancel_like_status_rows": cancel_like,
    })


def _status_contract() -> Dict[str, Any]:
    return {
        "SHOPEE": {
            "cancelled_statuses_exact": ["Đã hủy"],
            "normalization": "Unicode NFC + case-insensitive + trim/collapse whitespace; exact phrase only; no accent folding/substrings",
            "returns_refunds": "NOT_CANCELLED",
        },
        "TIKTOK": {
            "cancelled_statuses_exact": ["Canceled"],
            "normalization": "Unicode NFC + case-insensitive + trim/collapse whitespace; exact phrase only; no accent folding/substrings",
            "returns_refunds": "NOT_CANCELLED",
        },
        "NHANH": {
            "cancelled_statuses_exact": list(NHANH_CANCELLED_STATUSES_EXACT),
            "normalization": "Unicode NFC + case-insensitive + trim/collapse whitespace; exact normalized phrase only; no accent folding/substrings",
            "return_statuses_exact_not_cancelled": list(NHANH_RETURN_STATUSES_EXACT),
            "failed_statuses_exact_not_cancelled": list(NHANH_FAILED_STATUSES_EXACT),
            "successful_status_exact_informational": NHANH_SUCCESS_STATUS_EXACT,
            "success_orders_compatibility": "success_orders means non-cancelled orders for cross-channel reconciliation; successful_orders_exact is literal Thành công",
        },
    }


def _is_zero_financial_row(row: Mapping[str, Any]) -> bool:
    numeric = (
        "gross_sales", "net_sales", "orders", "cancelled_orders", "gm1", "cm1", "cm2", "profit",
        "ads_total", "backoffice_cost", "cogs_total",
    )
    return all(abs(float(row.get(k) or 0.0)) < 1e-12 for k in numeric)


def _zero_fill_rate_fields(row: Dict[str, Any]) -> None:
    if bool(row.get("zero_filled")) or _is_zero_financial_row(row):
        for field in ("cancellation_rate", "gm1_margin", "cm1_margin", "cm2_margin", "profit_margin"):
            if row.get(field) is None:
                row[field] = 0.0


def _zero_fill_package(package: Dict[str, Any]) -> None:
    # Parent MTD daily rows.
    detail = package.get("mtd_daily_detail_by_channel") or {}
    if isinstance(detail, dict):
        for rows in detail.values():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        _zero_fill_rate_fields(row)

    # Current/previous daily P&L rows.
    for block_name in ("daily_pnl_by_channel",):
        for item in package.get(block_name, []) if isinstance(package.get(block_name), list) else []:
            if isinstance(item, dict):
                for side in ("current", "previous"):
                    if isinstance(item.get(side), dict):
                        _zero_fill_rate_fields(item[side])
    dpmap = package.get("daily_pnl_by_channel_map") or {}
    if isinstance(dpmap, dict):
        for item in dpmap.values():
            if isinstance(item, dict):
                for side in ("current", "previous"):
                    if isinstance(item.get(side), dict):
                        _zero_fill_rate_fields(item[side])

    # Nhanh leaf daily rows and zero-sales summary rows.
    leaf_daily = package.get("nhanh_leaf_daily_detail") or {}
    if isinstance(leaf_daily, dict):
        for rows in leaf_daily.values():
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict):
                        _zero_fill_rate_fields(row)
    internal = package.get("nhanh_leaf_internal_cost_bucket") or {}
    if isinstance(internal, dict):
        for row in internal.get("daily", []) if isinstance(internal.get("daily"), list) else []:
            if isinstance(row, dict):
                _zero_fill_rate_fields(row)
    leaf_map = package.get("nhanh_leaf_channel_map") or {}
    if isinstance(leaf_map, dict):
        for item in leaf_map.values():
            if isinstance(item, dict):
                for side in ("current", "previous", "mtd", "wtd"):
                    if isinstance(item.get(side), dict):
                        _zero_fill_rate_fields(item[side])

    # Daily current-vs-previous trend rows: null means no source/no sales for that side.
    trend_fields = (
        "current_gross_sales", "previous_gross_sales", "current_net_sales", "previous_net_sales",
        "current_profit", "previous_profit", "current_orders", "previous_orders",
    )
    for block_name in ("weekly_channel_sales_trend_current_vs_previous", "monthly_channel_sales_trend_current_vs_previous"):
        rows = package.get(block_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            for field in trend_fields:
                if row.get(field) is None:
                    row[field] = 0.0
            row["net_sales_delta"] = float(row.get("current_net_sales") or 0.0) - float(row.get("previous_net_sales") or 0.0)

    # Rebuild the channel chart copies from cleaned trend rows.
    chart = package.get("chart_data")
    if isinstance(chart, dict):
        mapping = {
            "weekly_channel_net_sales_current_vs_previous": "weekly_channel_sales_trend_current_vs_previous",
            "monthly_channel_net_sales_current_vs_previous": "monthly_channel_sales_trend_current_vs_previous",
        }
        for chart_key, source_key in mapping.items():
            source = package.get(source_key)
            if not isinstance(source, list):
                continue
            chart[chart_key] = [
                {
                    "channel": row.get("channel"),
                    "day_no": row.get("day_no"),
                    "current_date": row.get("current_date"),
                    "previous_date": row.get("previous_date"),
                    "current_net_sales": float(row.get("current_net_sales") or 0.0),
                    "previous_net_sales": float(row.get("previous_net_sales") or 0.0),
                    "net_sales_delta": float(row.get("current_net_sales") or 0.0) - float(row.get("previous_net_sales") or 0.0),
                }
                for row in source if isinstance(row, dict)
            ]


def _rewrite_source_maps(obj: Any) -> None:
    if isinstance(obj, dict):
        sm = obj.get("source_map")
        if isinstance(sm, dict) and "cancelled_orders" in sm and "NHANH" in str(obj.get("channel", "NHANH")):
            sm["cancelled_orders"] = (
                "exact normalized statuses after Unicode NFC + case-insensitive + trim/collapse whitespace: "
                + " | ".join(NHANH_CANCELLED_STATUSES_EXACT)
            )
        for v in obj.values():
            _rewrite_source_maps(v)
    elif isinstance(obj, list):
        for v in obj:
            _rewrite_source_maps(v)


def _postprocess_package(package: Dict[str, Any], report_date: dt.date) -> Dict[str, Any]:
    meta = package.setdefault("metadata", {})
    meta.update({
        "package_version": PACKAGE_VERSION,
        "package_version_int": PACKAGE_VERSION_INT,
        "report_title": "CEO Daily P&L & Growth Package v4.6.1 — Clean Status Semantics + Zero-Fill Contract",
        "scope": "Shopee exact Đã hủy; TikTok exact Canceled; Nhanh normalized-exact cancel/return/failure statuses; Nhanh parent+leaf; sold/gift COGS; zero-filled trend/chart; Gross→Net Sales→GM1→CM1→CM2→Profit.",
    })

    dq = package.setdefault("data_quality", {})
    contract = _status_contract()
    dq["status_rule_by_channel"] = contract
    package["status_rule_by_channel"] = contract
    dq["audit_periods"] = [
        {"label": label, "start": a.isoformat(), "end": b.isoformat()}
        for a, b, label in _AUDIT_PERIODS
    ]
    dq["zero_fill_policy"] = "NO_SALES_OR_NO_ROW_TO_NUMERIC_ZERO_FOR_TABLES_AND_CHARTS"
    dq["nhanh_db_is_cancelled_used_for_report"] = False

    nhanh_audit = package.get("nhanh_status_audit") or {}
    warnings = [
        str(w) for w in (dq.get("warnings") or [])
        if not str(w).startswith("NHANH has ")
    ]
    errors = list(dq.get("errors") or [])
    # Remove only stale v4.6 Nhanh semantic errors/warnings if present; rebuild them below.
    errors = [e for e in errors if "NHANH" not in str(e) or "unmapped cancel-like" not in str(e)]

    mismatch = int(float(nhanh_audit.get("db_flag_mismatch_count") or 0)) if nhanh_audit else 0
    if mismatch:
        warnings.append(
            f"NHANH has {mismatch} legacy DB is_cancelled flags that differ from v4.6.1 normalized-exact semantics. "
            "The v4.6.1 report remains correct because Nhanh cancellation is recomputed directly from raw status; re-import Nhanh to clean the stored flag."
        )
    unmapped = nhanh_audit.get("unmapped_cancel_like_status_rows") or []
    if unmapped:
        errors.append(
            "NHANH has genuine cancel-like raw status tokens outside the approved normalized-exact whitelist. "
            "Review nhanh_status_audit.unmapped_cancel_like_status_rows and approve the business mapping before sending the package."
        )

    dq["warnings"] = list(dict.fromkeys(warnings))
    dq["errors"] = list(dict.fromkeys(str(e) for e in errors))
    dq["status"] = "FAIL" if dq["errors"] else ("WARNING" if dq["warnings"] else "PASS")
    dq["can_send"] = not bool(dq["errors"])

    render = package.setdefault("report_rendering_policy", {})
    render["status_rule_by_channel"] = contract
    render.pop("marketplace_status_rule", None)
    render["growth_override_rule"] = (
        "NHANH financial/status metrics (Gross, Net, orders, cancellation, COGS, GM, shipping, Backoffice, Ads, packaging and Profit) plus marketplace Gross/order/cancel metrics in inherited v3.3 blocks are harmonized from the same v4.6.1 direct daily sources and channel-specific status rules."
    )
    cogs = render.setdefault("cogs_split_sources", {})
    cogs["TIKTOK"] = "tiktok.order_items.price=0 => gift; price<>0 => sold; exact normalized Canceled => cancelled-order COGS treatment from TikTok ETL"
    render["zero_fill_policy"] = (
        "No-sales/no-source date points used in tables and charts are numeric 0, not null/N/A. True non-applicable or undefined comparison fields may remain null."
    )
    render["null_na_policy"] = (
        "Use 0 only for applicable numeric metrics on explicit zero-filled/no-sales rows and chart points. Keep true N/A/undefined values null and omit non-applicable cost lines."
    )
    render["order_status_display_policy"] = {
        "orders": "Tổng đơn",
        "cancelled_orders": "Đơn hủy",
        "success_orders": "Đơn không hủy (compatibility field; do not label as literal Đơn thành công for Nhanh)",
        "nhanh_non_cancelled_orders": "Đơn không hủy",
        "nhanh_successful_orders_exact": "Đơn trạng thái Thành công",
        "nhanh_returned_orders_status": "Đơn trạng thái hoàn (không phải đơn hủy)",
        "nhanh_failed_orders_status": "Đơn trạng thái Thất bại (không phải đơn hủy)",
    }

    rules = package.setdefault("pnl_rules", {})
    rules.update({
        "cancelled_status": "Channel-specific normalized exact status only: Shopee=Đã hủy; TikTok=Canceled; Nhanh=Đã hủy/Hệ Thống hủy/Khách hủy/HVC hủy. No fuzzy/substring matching.",
        "returns_refunds": "Shopee/TikTok return-refund classification is not counted as cancelled. Nhanh Đang hoàn/Xác nhận hoàn/Đã hoàn are informational return-status buckets and are not cancelled_orders.",
        "cogs_split": "NHANH uses nhanh.order_items.is_gift; SHOPEE uses shopee.order_items.is_gift; TIKTOK uses item price=0 as gift and price<>0 as sold, with exact normalized Canceled using the TikTok cancelled-order COGS treatment. sold_cogs + gift_cogs must equal cogs_total or package FAILS.",
        "cm1": "GM1 - platform/payment/fulfilment/packaging/logistics/cancel-related shipping costs according to each channel's cancellation rule.",
        "growth_harmonization": "Inherited v3.3 NHANH is fully overwritten from the same v4.6.1 direct daily P&L for Gross, Net, orders, cancellation, COGS, GM, shipping, Backoffice, Ads, packaging and Profit; SHOPEE/TIKTOK retain the channel-specific direct Gross/order/cancel harmonization from v4.6.",
        "all_channel_zero_fill": "Applicable numeric metrics on no-sales/no-source daily rows and channel chart points are emitted as 0; true N/A remains null.",
        "order_count_semantics": "Across channel reconciliation, success_orders means non-cancelled orders. For Nhanh literal status Thành công, use successful_orders_exact; never relabel success_orders as Đơn thành công.",
        "nhanh_returns": "Đang hoàn/Xác nhận hoàn/Đã hoàn are normalized-exact return-status audit buckets, not cancelled_orders. The +25,000 shipping rule remains for normalized-exact Đã hoàn.",
        "nhanh_failed": "Thất bại is a normalized-exact informational failed status, not cancelled_orders. No new P&L treatment is invented.",
        "nhanh_parent_daily": "Parent NHANH daily P&L is direct from nhanh.orders/order_items/ads_costs and reconciles to the seven exact leaf labels plus hidden _UNALLOCATED cost bucket.",
    })

    metric_rules = package.setdefault("metric_rules", {})
    metric_rules["order_status_display_rule"] = (
        "Render success_orders as Đơn không hủy. On Nhanh, render successful_orders_exact separately as Đơn trạng thái Thành công; return/failure status buckets are informational and not cancellations."
    )

    package["display_semantics"] = {
        "version": PACKAGE_VERSION,
        "success_orders": "non_cancelled_compatibility",
        "success_orders_display_label": "Đơn không hủy",
        "NHANH": {
            "successful_orders_exact_field": "successful_orders_exact",
            "successful_orders_exact_display_label": "Đơn trạng thái Thành công",
            "returned_orders_status_field": "returned_orders_status",
            "returned_orders_status_display_label": "Đơn trạng thái hoàn",
            "failed_orders_status_field": "failed_orders_status",
            "failed_orders_status_display_label": "Đơn trạng thái Thất bại",
        },
    }

    _zero_fill_package(package)
    _rewrite_source_maps(package)

    # Keep alias exact after zero-fill modifications.
    detail = package.get("mtd_daily_detail_by_channel") or {}
    if isinstance(detail, dict) and isinstance(detail.get("NHANH"), list):
        package["nhanh_parent_daily_detail"] = copy.deepcopy(detail["NHANH"])

    return package




def _nhanh_growth_day(base, nhanh_map: Mapping[str, Mapping[str, Any]], day: dt.date) -> Dict[str, float]:
    """Return the authoritative normalized Nhanh P&L metrics for one day.

    This deliberately goes through the same compose_pnl path used by the v4.6.1
    daily P&L package.  Therefore growth/week/month blocks cannot retain a
    different Backoffice/Profit result after a status is reclassified.
    """
    detail = dict(nhanh_map.get(day.isoformat(), {}) or {})
    pnl = base.compose_pnl(base.blank_base(day, "NHANH"), detail, _ACTIVE_VAT_RATES)
    net = base.safe_float(pnl.get("net_sales"))
    gross_margin = base.safe_float(pnl.get("gm1"))
    profit = base.safe_float(pnl.get("profit"))
    ads = base.safe_float(pnl.get("ads_total"))
    orders = base.safe_float(pnl.get("orders"))
    cancelled = base.safe_float(pnl.get("cancelled_orders"))
    return {
        "gross_sales": base.safe_float(pnl.get("gross_sales")),
        "net_sales": net,
        "orders": orders,
        "success_orders": base.safe_float(pnl.get("success_orders")),
        "cancelled_orders": cancelled,
        "cogs": base.safe_float(pnl.get("cogs_total")),
        "gross_margin": gross_margin,
        "platform_fees": 0.0,
        "shipping_return_fee": base.safe_float(pnl.get("logistics_order_cost")) + base.safe_float(pnl.get("cancel_related_shipping_fee")),
        "backoffice_fee": base.safe_float(pnl.get("backoffice_cost")),
        "ads_cost": ads,
        "live_cost": base.safe_float(pnl.get("livestream_inhouse")),
        "booking_fee": base.safe_float(pnl.get("booking_kol_koc")),
        "packaging_cost": base.safe_float(pnl.get("packaging_cost")),
        "profit": profit,
        "gm_pct": base.ratio(gross_margin, net),
        "profit_margin_pct": base.ratio(profit, net),
        "ads_sales_pct": base.ratio(ads, net),
        "cancellation_rate": base.ratio(cancelled, orders),
    }


_GROWTH_ADDITIVE = (
    "gross_sales", "net_sales", "orders", "success_orders", "cancelled_orders",
    "cogs", "gross_margin", "platform_fees", "shipping_return_fee",
    "backoffice_fee", "ads_cost", "live_cost", "booking_fee",
    "packaging_cost", "profit",
)


def _nhanh_growth_period(base, nhanh_map: Mapping[str, Mapping[str, Any]], start: dt.date, end: dt.date) -> Dict[str, float]:
    out = {k: 0.0 for k in _GROWTH_ADDITIVE}
    for day in base.date_range(start, end):
        d = _nhanh_growth_day(base, nhanh_map, day)
        for key in _GROWTH_ADDITIVE:
            out[key] += base.safe_float(d.get(key))
    out["gm_pct"] = base.ratio(out["gross_margin"], out["net_sales"])
    out["profit_margin_pct"] = base.ratio(out["profit"], out["net_sales"])
    out["ads_sales_pct"] = base.ratio(out["ads_cost"], out["net_sales"])
    out["cancellation_rate"] = base.ratio(out["cancelled_orders"], out["orders"])
    return out


def _write_period_metrics(base, row: Dict[str, Any], stats: Mapping[str, float], suffix: str) -> None:
    for metric in _GROWTH_ADDITIVE:
        key = f"{metric}_{suffix}"
        if key in row:
            row[key] = base.safe_float(stats.get(metric))
    for metric in ("gm_pct", "profit_margin_pct", "ads_sales_pct", "cancellation_rate"):
        key = f"{metric}_{suffix}"
        if key in row:
            row[key] = base.safe_float(stats.get(metric))


def _sum_period_from_channel_rows(base, rows: Sequence[Mapping[str, Any]], suffix: str) -> Dict[str, float]:
    stats = {k: 0.0 for k in _GROWTH_ADDITIVE}
    for row in rows:
        if base.normalize_channel(row.get("channel")) == "TOTAL":
            continue
        for metric in _GROWTH_ADDITIVE:
            stats[metric] += base.safe_float(row.get(f"{metric}_{suffix}"))
    stats["gm_pct"] = base.ratio(stats["gross_margin"], stats["net_sales"])
    stats["profit_margin_pct"] = base.ratio(stats["profit"], stats["net_sales"])
    stats["ads_sales_pct"] = base.ratio(stats["ads_cost"], stats["net_sales"])
    stats["cancellation_rate"] = base.ratio(stats["cancelled_orders"], stats["orders"])
    return stats


def _write_comparison_metrics(base, row: Dict[str, Any], cur: Mapping[str, float], prev: Mapping[str, float], suffix: str) -> None:
    # Current values.
    _write_period_metrics(base, row, cur, suffix)
    # Previous values. Some metrics (success_orders) are intentionally absent
    # in inherited comparison schemas; only write keys that exist.
    for metric in _GROWTH_ADDITIVE:
        key = f"{metric}_prev_{suffix}"
        if key in row:
            row[key] = base.safe_float(prev.get(metric))
    for metric in ("gm_pct", "profit_margin_pct", "ads_sales_pct", "cancellation_rate"):
        key = f"{metric}_prev_{suffix}"
        if key in row:
            row[key] = base.safe_float(prev.get(metric))

    # Recompute every comparison delta exposed by the v3.3 schema.
    pairs = (
        ("gross_sales", "gross_sales_delta", "gross_sales_delta_pct"),
        ("net_sales", "net_sales_delta", "net_sales_delta_pct"),
        ("orders", "orders_delta", "orders_delta_pct"),
        ("cogs", "cogs_delta", None),
        ("gross_margin", "gross_margin_delta", None),
        ("ads_cost", "ads_cost_delta", None),
        ("profit", "profit_delta", "profit_delta_pct"),
    )
    for metric, dkey, pkey in pairs:
        ck = f"{metric}_{suffix}"
        pk = f"{metric}_prev_{suffix}"
        if ck in row and pk in row and dkey in row:
            row[dkey] = base.safe_float(row.get(ck)) - base.safe_float(row.get(pk))
            if pkey and pkey in row:
                row[pkey] = base.delta_pct(row.get(ck), row.get(pk))


def _daily_growth_write(base, row: Dict[str, Any], stats: Mapping[str, float]) -> None:
    for metric in _GROWTH_ADDITIVE:
        if metric in row:
            row[metric] = base.safe_float(stats.get(metric))
    for metric in ("gm_pct", "profit_margin_pct", "ads_sales_pct", "cancellation_rate"):
        if metric in row:
            row[metric] = base.safe_float(stats.get(metric))


def _daily_total_from_members(base, members: Sequence[Mapping[str, Any]]) -> Dict[str, float]:
    stats = {k: 0.0 for k in _GROWTH_ADDITIVE}
    for row in members:
        for metric in _GROWTH_ADDITIVE:
            stats[metric] += base.safe_float(row.get(metric))
    stats["gm_pct"] = base.ratio(stats["gross_margin"], stats["net_sales"])
    stats["profit_margin_pct"] = base.ratio(stats["profit"], stats["net_sales"])
    stats["ads_sales_pct"] = base.ratio(stats["ads_cost"], stats["net_sales"])
    stats["cancellation_rate"] = base.ratio(stats["cancelled_orders"], stats["orders"])
    return stats


def _update_forecast_row(base, row: Dict[str, Any], net_sales: float, profit: float) -> None:
    days_elapsed = base.safe_float(row.get("days_elapsed"))
    days_in_month = base.safe_float(row.get("days_in_month"))
    row["net_sales_mtd"] = net_sales
    row["profit_mtd"] = profit
    rr_net = net_sales / days_elapsed if days_elapsed else 0.0
    rr_profit = profit / days_elapsed if days_elapsed else 0.0
    row["run_rate_net_sales_per_day"] = rr_net
    row["run_rate_profit_per_day"] = rr_profit
    row["forecast_net_sales_month_end"] = rr_net * days_in_month
    row["forecast_profit_month_end"] = rr_profit * days_in_month
    if row.get("target_net_sales") not in (None, 0, 0.0):
        row["target_completion_pct"] = base.ratio(net_sales, row.get("target_net_sales"))
        row["forecast_vs_target_gap"] = row["forecast_net_sales_month_end"] - base.safe_float(row.get("target_net_sales"))


def _harmonize_growth_nhanh_v461(base, growth: Dict[str, Any], nhanh_map: Mapping[str, Mapping[str, Any]], report_date: dt.date) -> None:
    """Fully harmonize Nhanh financial + status metrics across inherited v3.3.

    v4.6 harmonized only Gross/orders/cancellation.  That is not sufficient once
    harmless status normalization can move a Nhanh order into the cancelled
    set, because Backoffice and therefore Profit can also change.  v4.6.1 uses
    one authoritative direct Nhanh P&L source for daily, WTD, MTD, comparisons,
    totals, forecasts and chart contracts.
    """
    if not growth:
        return
    week_start = report_date - dt.timedelta(days=report_date.weekday())
    prev_week_start = week_start - dt.timedelta(days=7)
    prev_week_end = prev_week_start + (report_date - week_start)
    mstart = report_date.replace(day=1)
    prev_same_day = _add_months(report_date, -1)
    prev_mstart = prev_same_day.replace(day=1)
    periods = {
        "weekly": (week_start, report_date, prev_week_start, prev_week_end, "wtd"),
        "monthly": (mstart, report_date, prev_mstart, prev_same_day, "mtd"),
    }

    # Scorecards and summaries.
    for prefix, (cur_start, cur_end, prev_start, prev_end, suffix) in periods.items():
        score_rows = growth.get(f"{prefix}_scorecard_by_channel")
        if isinstance(score_rows, list):
            nrow = next((r for r in score_rows if base.normalize_channel(r.get("channel")) == "NHANH"), None)
            if isinstance(nrow, dict):
                nstats = _nhanh_growth_period(base, nhanh_map, cur_start, cur_end)
                _write_period_metrics(base, nrow, nstats, suffix)
                if prefix == "monthly":
                    days_elapsed = base.safe_float(nrow.get("days_elapsed"))
                    days_in_month = base.safe_float(nrow.get("days_in_month"))
                    if "run_rate_net_sales_per_day" in nrow:
                        nrow["run_rate_net_sales_per_day"] = nstats["net_sales"] / days_elapsed if days_elapsed else 0.0
                    if "run_rate_profit_per_day" in nrow:
                        nrow["run_rate_profit_per_day"] = nstats["profit"] / days_elapsed if days_elapsed else 0.0
                    if "forecast_net_sales_month_end" in nrow:
                        nrow["forecast_net_sales_month_end"] = (nstats["net_sales"] / days_elapsed * days_in_month) if days_elapsed else 0.0
                    if "forecast_profit_month_end" in nrow:
                        nrow["forecast_profit_month_end"] = (nstats["profit"] / days_elapsed * days_in_month) if days_elapsed else 0.0

            total_stats = _sum_period_from_channel_rows(base, score_rows, suffix)
            summary = growth.get(f"{prefix}_summary")
            if isinstance(summary, dict):
                _write_period_metrics(base, summary, total_stats, suffix)
                if prefix == "monthly":
                    days_elapsed = base.safe_float(summary.get("days_elapsed"))
                    days_in_month = base.safe_float(summary.get("days_in_month"))
                    summary["run_rate_net_sales_per_day"] = total_stats["net_sales"] / days_elapsed if days_elapsed else 0.0
                    summary["run_rate_profit_per_day"] = total_stats["profit"] / days_elapsed if days_elapsed else 0.0
                    summary["forecast_net_sales_month_end"] = (total_stats["net_sales"] / days_elapsed * days_in_month) if days_elapsed else 0.0
                    summary["forecast_profit_month_end"] = (total_stats["profit"] / days_elapsed * days_in_month) if days_elapsed else 0.0

            if prefix == "monthly":
                total_net = sum(base.safe_float(r.get("net_sales_mtd")) for r in score_rows if base.normalize_channel(r.get("channel")) != "TOTAL")
                for r in score_rows:
                    if "channel_share_pct" in r:
                        r["channel_share_pct"] = base.ratio(r.get("net_sales_mtd"), total_net)

        # Comparison by channel and total comparison.
        comp_rows = growth.get(f"{prefix}_comparison_by_channel")
        if isinstance(comp_rows, list):
            cur_stats = _nhanh_growth_period(base, nhanh_map, cur_start, cur_end)
            prev_stats = _nhanh_growth_period(base, nhanh_map, prev_start, prev_end)
            ncomp = next((r for r in comp_rows if base.normalize_channel(r.get("channel")) == "NHANH"), None)
            if isinstance(ncomp, dict):
                _write_comparison_metrics(base, ncomp, cur_stats, prev_stats, suffix)

            total_key = f"{prefix}_comparison_vs_previous_" + ("week_same_days" if prefix == "weekly" else "month_same_period")
            total_comp = growth.get(total_key)
            if isinstance(total_comp, dict):
                # Build total current/previous stats from channel comparison rows.
                cur_total = {k: 0.0 for k in _GROWTH_ADDITIVE}
                prev_total = {k: 0.0 for k in _GROWTH_ADDITIVE}
                for r in comp_rows:
                    if base.normalize_channel(r.get("channel")) == "TOTAL":
                        continue
                    for metric in _GROWTH_ADDITIVE:
                        cur_total[metric] += base.safe_float(r.get(f"{metric}_{suffix}"))
                        prev_total[metric] += base.safe_float(r.get(f"{metric}_prev_{suffix}"))
                for stats in (cur_total, prev_total):
                    stats["gm_pct"] = base.ratio(stats["gross_margin"], stats["net_sales"])
                    stats["profit_margin_pct"] = base.ratio(stats["profit"], stats["net_sales"])
                    stats["ads_sales_pct"] = base.ratio(stats["ads_cost"], stats["net_sales"])
                    stats["cancellation_rate"] = base.ratio(stats["cancelled_orders"], stats["orders"])
                _write_comparison_metrics(base, total_comp, cur_total, prev_total, suffix)

    # Authoritative Nhanh daily trend, then TOTAL daily trend.
    for block_name in ("weekly_daily_trend", "previous_weekly_daily_trend", "monthly_daily_trend", "previous_monthly_daily_trend"):
        rows = growth.get(block_name)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if base.normalize_channel(row.get("channel")) == "NHANH" and row.get("trend_date"):
                _daily_growth_write(base, row, _nhanh_growth_day(base, nhanh_map, base.parse_date(str(row["trend_date"]))))
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        totals: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("trend_date"))
            if base.normalize_channel(row.get("channel")) == "TOTAL":
                totals[key] = row
            else:
                by_date.setdefault(key, []).append(row)
        for key, total in totals.items():
            _daily_growth_write(base, total, _daily_total_from_members(base, by_date.get(key, [])))

    # Current-vs-previous channel trend plus total trend.
    for prefix in ("weekly", "monthly"):
        channel_rows = growth.get(f"{prefix}_channel_sales_trend_current_vs_previous")
        total_rows = growth.get(f"{prefix}_total_sales_trend_current_vs_previous")
        if not isinstance(channel_rows, list):
            continue
        for row in channel_rows:
            if base.normalize_channel(row.get("channel")) != "NHANH":
                continue
            for side in ("current", "previous"):
                dkey = f"{side}_date"
                if not row.get(dkey):
                    continue
                d = _nhanh_growth_day(base, nhanh_map, base.parse_date(str(row[dkey])))
                for metric in ("net_sales", "gross_sales", "profit", "orders"):
                    key = f"{side}_{metric}"
                    if key in row:
                        row[key] = base.safe_float(d.get(metric))
            if "net_sales_delta" in row:
                row["net_sales_delta"] = base.safe_float(row.get("current_net_sales")) - base.safe_float(row.get("previous_net_sales"))
        if isinstance(total_rows, list):
            grouped: Dict[int, List[Dict[str, Any]]] = {}
            for row in channel_rows:
                grouped.setdefault(base.safe_int(row.get("day_no")), []).append(row)
            for row in total_rows:
                peers = grouped.get(base.safe_int(row.get("day_no")), [])
                for side in ("current", "previous"):
                    for metric in ("net_sales", "gross_sales", "profit", "orders"):
                        key = f"{side}_{metric}"
                        if key in row:
                            row[key] = sum(base.safe_float(r.get(key)) for r in peers)
                if "net_sales_delta" in row:
                    row["net_sales_delta"] = base.safe_float(row.get("current_net_sales")) - base.safe_float(row.get("previous_net_sales"))

    # Monthly chart/helper blocks sourced from monthly daily/scorecard data.
    monthly_daily = growth.get("monthly_daily_trend") if isinstance(growth.get("monthly_daily_trend"), list) else []
    daily_map = {(str(r.get("trend_date")), base.normalize_channel(r.get("channel"))): r for r in monthly_daily}
    total_daily_map = {d: r for (d, ch), r in daily_map.items() if ch == "TOTAL"}

    for row in growth.get("monthly_channel_sales_trend_chart", []) if isinstance(growth.get("monthly_channel_sales_trend_chart"), list) else []:
        if base.normalize_channel(row.get("channel")) == "NHANH":
            src = daily_map.get((str(row.get("trend_date")), "NHANH"))
            if src:
                for key in ("gross_sales", "net_sales", "orders", "profit", "profit_margin_pct"):
                    if key in row:
                        row[key] = src.get(key, 0.0)
    for row in growth.get("monthly_total_sales_trend_chart", []) if isinstance(growth.get("monthly_total_sales_trend_chart"), list) else []:
        src = total_daily_map.get(str(row.get("trend_date")))
        if src:
            for key in ("gross_sales", "net_sales", "orders", "profit"):
                if key in row:
                    row[key] = src.get(key, 0.0)

    monthly_score = growth.get("monthly_scorecard_by_channel") if isinstance(growth.get("monthly_scorecard_by_channel"), list) else []
    score_map = {base.normalize_channel(r.get("channel")): r for r in monthly_score}
    total_net = sum(base.safe_float(r.get("net_sales_mtd")) for r in monthly_score if base.normalize_channel(r.get("channel")) != "TOTAL")
    total_profit = sum(base.safe_float(r.get("profit_mtd")) for r in monthly_score if base.normalize_channel(r.get("channel")) != "TOTAL")
    min_profit = min([base.safe_float(r.get("profit_mtd")) for r in monthly_score if base.normalize_channel(r.get("channel")) != "TOTAL"] or [0.0])
    can_profit_pie = total_profit > 0 and min_profit >= 0
    growth_dq = growth.get("data_quality")
    if isinstance(growth_dq, dict):
        growth_dq["profit_pie_chart_usable"] = can_profit_pie

    share_rows = growth.get("monthly_channel_sales_share_chart")
    if isinstance(share_rows, list):
        for row in share_rows:
            src = score_map.get(base.normalize_channel(row.get("channel")))
            if src:
                if "gross_sales_mtd" in row: row["gross_sales_mtd"] = src.get("gross_sales_mtd", 0.0)
                if "net_sales_mtd" in row: row["net_sales_mtd"] = src.get("net_sales_mtd", 0.0)
                if "channel_share_pct" in row: row["channel_share_pct"] = base.ratio(src.get("net_sales_mtd"), total_net)
                if "channel_share_percent" in row: row["channel_share_percent"] = base.ratio(src.get("net_sales_mtd"), total_net) * 100

    profit_rows = growth.get("monthly_channel_profit_share_chart")
    if isinstance(profit_rows, list):
        for row in profit_rows:
            src = score_map.get(base.normalize_channel(row.get("channel")))
            if src:
                profit = base.safe_float(src.get("profit_mtd"))
                if "profit_mtd" in row: row["profit_mtd"] = profit
                if "profit_margin_pct_mtd" in row: row["profit_margin_pct_mtd"] = src.get("profit_margin_pct_mtd", 0.0)
                if "total_profit_mtd" in row: row["total_profit_mtd"] = total_profit
                if "min_channel_profit_mtd" in row: row["min_channel_profit_mtd"] = min_profit
                share = base.ratio(profit, total_profit) if can_profit_pie else None
                if "profit_share_pct" in row: row["profit_share_pct"] = share
                if "profit_share_percent" in row: row["profit_share_percent"] = (share * 100 if share is not None else None)
                if "can_use_profit_pie_chart" in row: row["can_use_profit_pie_chart"] = can_profit_pie
                if "chart_note" in row:
                    row["chart_note"] = "Profit pie usable" if can_profit_pie else "At least one channel has negative profit; use bar chart instead of pie."

    # Forecast rows.
    forecast_rows = growth.get("monthly_target_forecast_by_channel")
    if isinstance(forecast_rows, list):
        for row in forecast_rows:
            src = score_map.get(base.normalize_channel(row.get("channel")))
            if src and base.normalize_channel(row.get("channel")) == "NHANH":
                _update_forecast_row(base, row, base.safe_float(src.get("net_sales_mtd")), base.safe_float(src.get("profit_mtd")))
    total_forecast = growth.get("monthly_target_forecast")
    msummary = growth.get("monthly_summary")
    if isinstance(total_forecast, dict) and isinstance(msummary, dict):
        _update_forecast_row(base, total_forecast, base.safe_float(msummary.get("net_sales_mtd")), base.safe_float(msummary.get("profit_mtd")))

    # Chart_data mirrors.  Refresh from the authoritative top-level blocks so
    # AI/Excel cannot see stale values that disagree with scorecards.
    chart = growth.get("chart_data")
    if isinstance(chart, dict):
        weekly_totals = {str(r.get("trend_date")): r for r in growth.get("weekly_daily_trend", []) if base.normalize_channel(r.get("channel")) == "TOTAL"}
        for row in chart.get("weekly_net_sales_trend", []) if isinstance(chart.get("weekly_net_sales_trend"), list) else []:
            src = weekly_totals.get(str(row.get("date")))
            if src:
                for ck, sk in (("net_sales","net_sales"),("gross_sales","gross_sales"),("profit","profit"),("orders","orders")):
                    if ck in row: row[ck] = src.get(sk, 0.0)
        monthly_totals = {str(r.get("trend_date")): r for r in monthly_daily if base.normalize_channel(r.get("channel")) == "TOTAL"}
        for row in chart.get("monthly_net_sales_trend", []) if isinstance(chart.get("monthly_net_sales_trend"), list) else []:
            src = monthly_totals.get(str(row.get("date")))
            if src:
                for ck, sk in (("net_sales","net_sales"),("gross_sales","gross_sales"),("profit","profit"),("orders","orders")):
                    if ck in row: row[ck] = src.get(sk, 0.0)
        for row in chart.get("monthly_channel_sales_lines", []) if isinstance(chart.get("monthly_channel_sales_lines"), list) else []:
            if base.normalize_channel(row.get("channel")) == "NHANH":
                src = daily_map.get((str(row.get("date")), "NHANH"))
                if src:
                    for ck in ("net_sales", "gross_sales", "profit", "orders"):
                        if ck in row: row[ck] = src.get(ck, 0.0)

        # Current-vs-previous chart blocks contain only Net Sales by contract.
        for prefix in ("weekly", "monthly"):
            top_ch = growth.get(f"{prefix}_channel_sales_trend_current_vs_previous")
            chart_ch = chart.get(f"{prefix}_channel_net_sales_current_vs_previous")
            if isinstance(top_ch, list) and isinstance(chart_ch, list):
                src_map = {(base.normalize_channel(r.get("channel")), base.safe_int(r.get("day_no"))): r for r in top_ch}
                for row in chart_ch:
                    src = src_map.get((base.normalize_channel(row.get("channel")), base.safe_int(row.get("day_no"))))
                    if src:
                        for key in ("current_net_sales", "previous_net_sales", "net_sales_delta"):
                            if key in row: row[key] = src.get(key, 0.0)
            top_total = growth.get(f"{prefix}_total_sales_trend_current_vs_previous")
            chart_total = chart.get(f"{prefix}_total_net_sales_current_vs_previous")
            if isinstance(top_total, list) and isinstance(chart_total, list):
                src_map = {base.safe_int(r.get("day_no")): r for r in top_total}
                for row in chart_total:
                    src = src_map.get(base.safe_int(row.get("day_no")))
                    if src:
                        for key in ("current_net_sales", "previous_net_sales", "net_sales_delta"):
                            if key in row: row[key] = src.get(key, 0.0)

        if isinstance(share_rows, list):
            chart["monthly_sales_share_pie"] = [
                {
                    "channel": r.get("channel"),
                    "net_sales_mtd": r.get("net_sales_mtd", 0.0),
                    "share_percent": r.get("channel_share_percent", base.safe_float(r.get("channel_share_pct")) * 100),
                }
                for r in share_rows
            ]
        if isinstance(profit_rows, list):
            chart["monthly_profit_share_or_bar"] = [
                {
                    "channel": r.get("channel"),
                    "profit_mtd": r.get("profit_mtd", 0.0),
                    "profit_share_percent": r.get("profit_share_percent"),
                    "profit_margin_pct": r.get("profit_margin_pct_mtd", 0.0),
                    "can_use_profit_pie_chart": can_profit_pie,
                    "chart_note": r.get("chart_note"),
                }
                for r in profit_rows
            ]
            chart["monthly_profit_pie_chart_usable"] = can_profit_pie


def _install_v461_overrides(v46, report_date: dt.date, compare_date: dt.date) -> None:
    global _AUDIT_PERIODS
    _AUDIT_PERIODS = _periods_for_report(v46, report_date, compare_date)

    # Replace v4.6 Nhanh status helpers with normalized-exact helpers.
    v46.sql_nhanh_cancelled = sql_nhanh_cancelled
    v46.sql_nhanh_return = sql_nhanh_return
    v46.sql_nhanh_return_in_progress = sql_nhanh_return_in_progress
    v46.sql_nhanh_failed = sql_nhanh_failed
    v46.sql_nhanh_no_bo = sql_nhanh_no_bo
    v46._status_contract = _status_contract

    # Direct Nhanh detail + audit need normalized success/return/cancel predicates.
    v46._fetch_nhanh_leaf_detail = lambda base, cur, start, end: _fetch_nhanh_leaf_detail_v461(v46, base, cur, start, end)
    v46._fetch_nhanh_status_audit = lambda base, cur, start, end: _fetch_nhanh_status_audit_v461(v46, base, cur, start, end)
    v46._fetch_marketplace_status_audit = lambda base, cur, start, end: _fetch_marketplace_status_audit_v461(v46, base, cur, start, end)
    v46._harmonize_growth_nhanh = _harmonize_growth_nhanh_v461


def build_package_v461(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    include_growth_context: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    v46 = _load_v46()
    # Resolve dates using v4.6's v4.5 base utilities through a lightweight load.
    v45 = v46._load_base()
    report_date = v45.parse_date(report_date_str)
    compare_date = v45.resolve_compare_date(report_date, compare_mode, explicit_compare_date)
    global _ACTIVE_VAT_RATES
    _ACTIVE_VAT_RATES = {str(k): float(v) for k, v in dict(vat_rates or {}).items()}
    _install_v461_overrides(v46, report_date, compare_date)

    base, package = v46.build_package_v46(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        include_growth_context=include_growth_context,
    )
    package = _postprocess_package(package, report_date)
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    return base, base.clean(package)


def _count_nulls_in_chart_contract(package: Mapping[str, Any]) -> int:
    count = 0
    for block in ("weekly_channel_sales_trend_current_vs_previous", "monthly_channel_sales_trend_current_vs_previous"):
        for row in package.get(block, []) if isinstance(package.get(block), list) else []:
            for field in ("current_net_sales", "previous_net_sales", "net_sales_delta"):
                if row.get(field) is None:
                    count += 1
    chart = package.get("chart_data") or {}
    if isinstance(chart, dict):
        for block in ("weekly_channel_net_sales_current_vs_previous", "monthly_channel_net_sales_current_vs_previous"):
            for row in chart.get(block, []) if isinstance(chart.get(block), list) else []:
                for field in ("current_net_sales", "previous_net_sales", "net_sales_delta"):
                    if row.get(field) is None:
                        count += 1
    return count


def self_test() -> int:
    failures: List[str] = []
    # Exact normalized status behavior.
    cancel_variants = ["Hệ Thống hủy", "Hệ thống hủy", "  HỆ   THỐNG HỦY  "]
    canonical_cancel = {_canonical_status(x) for x in NHANH_CANCELLED_STATUSES_EXACT}
    if any(_canonical_status(x) not in canonical_cancel for x in cancel_variants):
        failures.append("Nhanh case/whitespace normalization failed")
    if _canonical_status("Đã huỷ") in canonical_cancel:
        failures.append("Accent folding must not occur")
    if is_cancel_like_status_text("Đang chuyển"):
        failures.append("Đang chuyển must not be a cancel-like diagnostic token")
    if not is_cancel_like_status_text("Hệ thống hủy"):
        failures.append("Hệ thống hủy must be a cancel-like diagnostic token")
    if "lower(regexp_replace" not in sql_nhanh_cancelled("o.status"):
        failures.append("Nhanh SQL normalization missing")

    # Offline zero-fill contract fixture.
    fixture: Dict[str, Any] = {
        "metadata": {},
        "data_quality": {"warnings": [], "errors": []},
        "mtd_daily_detail_by_channel": {"GT": [{"report_date": "2026-08-01", "zero_filled": True, "gross_sales": 0, "net_sales": 0, "orders": 0, "gm1": 0, "cm1": 0, "cm2": 0, "profit": 0, "cancellation_rate": None, "gm1_margin": None, "cm1_margin": None, "cm2_margin": None, "profit_margin": None}], "NHANH": []},
        "weekly_channel_sales_trend_current_vs_previous": [{"channel": "GT", "day_no": 1, "current_net_sales": None, "previous_net_sales": 10, "current_gross_sales": None, "previous_gross_sales": 12, "current_profit": None, "previous_profit": 2, "current_orders": None, "previous_orders": 1, "net_sales_delta": None}],
        "monthly_channel_sales_trend_current_vs_previous": [],
        "chart_data": {"weekly_channel_net_sales_current_vs_previous": []},
        "report_rendering_policy": {},
        "pnl_rules": {},
        "metric_rules": {},
        "nhanh_status_audit": {"db_flag_mismatch_count": 0, "unmapped_cancel_like_status_rows": []},
    }
    _postprocess_package(fixture, dt.date(2026, 8, 1))
    row = fixture["weekly_channel_sales_trend_current_vs_previous"][0]
    if row["current_net_sales"] != 0 or row["net_sales_delta"] != -10:
        failures.append("Trend zero-fill failed")
    if fixture["mtd_daily_detail_by_channel"]["GT"][0]["profit_margin"] != 0:
        failures.append("Zero-filled rate KPI failed")
    if _count_nulls_in_chart_contract(fixture):
        failures.append("Chart/trend contract still contains null")

    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for item in failures:
            print("-", item, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print(json.dumps(_status_contract(), ensure_ascii=False, indent=2))
    print("Zero-fill + metadata cleanup contract: PASS")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Report date YYYY-MM-DD")
    parser.add_argument("--compare-mode", choices=("previous_day", "previous_week_same_day", "previous_month_same_day"), default="previous_day")
    parser.add_argument("--compare-date", default=None)
    parser.add_argument("--vat-rates", default=os.getenv("DAILY_PNL_VAT_RATES", ""))
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

    v46 = _load_v46()
    v45 = v46._load_base()
    try:
        report_date = v45.parse_date(args.date).isoformat()
        if args.compare_date:
            v45.parse_date(args.compare_date)
        vat_rates = v45.parse_vat_rates(args.vat_rates)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.require_vat_rates:
        missing = [ch for ch in v45.DAILY_PNL_SHEETS if ch not in vat_rates]
        if missing:
            print("ERROR: missing VAT rates for " + ", ".join(missing), file=sys.stderr)
            return 2

    base, package = build_package_v461(
        report_date,
        args.compare_mode,
        args.compare_date,
        vat_rates,
        include_growth_context=not args.without_growth_context,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ceo_daily_pnl_package_v4_6_1_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_6_1_{report_date}.md"
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=base.json_default), encoding="utf-8")
    md = base.markdown_summary(package)
    md_path.write_text(md, encoding="utf-8")
    if args.store_db:
        base.PACKAGE_VERSION = PACKAGE_VERSION
        base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
        base.store_best_effort(report_date, package, md, str(json_path), str(md_path))

    dq = package.get("data_quality", {})
    print(f"JSON package: {json_path}")
    print(f"Markdown summary: {md_path}")
    print(f"Data quality: {dq.get('status')}")
    for warning in dq.get("warnings") or []:
        print(f"WARNING: {warning}")
    for error in dq.get("errors") or []:
        print(f"ERROR: {error}")
    return 0 if dq.get("can_send") else 1


if __name__ == "__main__":
    raise SystemExit(main())
