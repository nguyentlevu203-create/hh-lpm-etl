#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.6.

v4.6 is a compatibility layer on top of the deployed v4.5 builder.  It fixes
three production contracts without copying the full v4.5 implementation:

1) Channel-specific exact cancellation status
   - Shopee: exact normalized ``Đã hủy``
   - TikTok: exact normalized ``Canceled``
   - Nhanh: exact trimmed one of ``Đã hủy``, ``Hệ Thống hủy``, ``Khách hủy``,
     ``HVC hủy``.
   Return/refund statuses are never counted as cancelled.

2) Nhanh parent daily P&L is read directly from nhanh.orders/order_items/ads_costs
   for every day, while the existing seven exact leaf labels remain available.
   Parent and leaf data reconcile by construction.

3) Weekly/monthly inherited growth blocks are harmonized for Nhanh as well as
   Shopee/TikTok, so orders/gross/cancellation metrics use the same direct daily
   source throughout the package.

No EBITDA is introduced.  The P&L still ends at Profit/LỢI NHUẬN.
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

PACKAGE_VERSION = "v4.6"
PACKAGE_VERSION_INT = 46

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
# Preserve deployed BO logic and only add the newly-recognized HVC hủy alias.
# We intentionally do NOT invent new BO treatment for Đang hoàn/Xác nhận hoàn/Thất bại.
NHANH_NO_BO_STATUSES_EXACT: Tuple[str, ...] = tuple(
    dict.fromkeys((*NHANH_CANCELLED_STATUSES_EXACT, "Đã hoàn"))
)

BASE_FILENAME = "build_ceo_daily_pnl_package_v4_5.py"
EPSILON = 1.0


def _load_base():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. v4.6 is intentionally layered on the deployed v4.5 builder."
        )
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v45_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _sql_in(values: Sequence[str]) -> str:
    return "(" + ", ".join(_sql_literal(v) for v in values) + ")"


def sql_marketplace_cancelled(status_expr: str) -> str:
    """Channel-aware exact marketplace cancellation predicate.

    The deployed v4.5 builder passes TikTok's ``order_status`` column and
    Shopee's ``status`` column.  No fuzzy or substring matching is used.
    """
    normalized = (
        "lower(regexp_replace(btrim(COALESCE(" + status_expr + ",'')), "
        "'[[:space:]]+', ' ', 'g'))"
    )
    target = TIKTOK_CANCELLED_STATUS_EXACT if "order_status" in status_expr.lower() else SHOPEE_CANCELLED_STATUS_EXACT
    return f"{normalized} = {_sql_literal(target)}"


def sql_nhanh_cancelled(status_expr: str) -> str:
    # Nhanh contract preserves the ETL's exact-text semantics: outer trim only.
    return f"btrim(COALESCE({status_expr},'')) IN {_sql_in(NHANH_CANCELLED_STATUSES_EXACT)}"


def sql_nhanh_return(status_expr: str) -> str:
    return f"btrim(COALESCE({status_expr},'')) IN {_sql_in(NHANH_RETURN_STATUSES_EXACT)}"


def sql_nhanh_return_in_progress(status_expr: str) -> str:
    return f"btrim(COALESCE({status_expr},'')) IN {_sql_in(NHANH_RETURN_IN_PROGRESS_STATUSES_EXACT)}"


def sql_nhanh_failed(status_expr: str) -> str:
    return f"btrim(COALESCE({status_expr},'')) IN {_sql_in(NHANH_FAILED_STATUSES_EXACT)}"


def sql_nhanh_no_bo(status_expr: str) -> str:
    return f"btrim(COALESCE({status_expr},'')) IN {_sql_in(NHANH_NO_BO_STATUSES_EXACT)}"


def _fetch_nhanh_leaf_detail(base, cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Direct Nhanh P&L by exact label with separate cancel/return/failure status buckets."""
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
    success_exact = f"btrim(COALESCE(o.status,'')) = {_sql_literal(NHANH_SUCCESS_STATUS_EXACT)}"

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
            COUNT(*) FILTER (WHERE btrim(COALESCE(o.status,'')) = 'Đã hoàn')::numeric AS completed_return_orders,
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

    def map_leaf(source_label: str, *, is_ads: bool = False) -> str:
        if source_label in base.NHANH_LABEL_TO_KEY:
            return base.NHANH_LABEL_TO_KEY[source_label]
        # Unmapped/blank ads remain a hidden internal cost bucket.  Unmapped sales
        # will be caught by parent/leaf reconciliation + unallocated sales gate.
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
        leaf = map_leaf(label, is_ads=True)
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


def _fetch_nhanh_parent_detail(base, cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    """Aggregate the same direct leaf source to one Nhanh parent row per day."""
    leaf_rows = _fetch_nhanh_leaf_detail(base, cur, start, end)
    additive = (
        "gross_sales", "gross_non_cancelled_orders", "gross_cancelled_orders", "net_sales",
        "sold_cogs", "gift_cogs", "cogs_total", "shipping_return_fee", "backoffice_fee",
        "packaging_cost", "orders", "success_orders", "cancelled_orders", "order_count_detail",
        "non_cancelled_order_count_detail", "cancelled_order_count_detail", "cancelled_orders_with_gross",
        "successful_orders_exact", "returned_orders_status", "return_in_progress_orders",
        "completed_return_orders", "failed_orders_status", "ads_cost",
    )
    by_day: Dict[str, Dict[str, Any]] = {}
    for raw in leaf_rows:
        day = str(raw.get("report_date"))
        row = by_day.setdefault(day, {"report_date": day})
        for field in additive:
            row[field] = base.safe_float(row.get(field)) + base.safe_float(raw.get(field))
    out = []
    for row in by_day.values():
        row["gross_all_orders"] = row.get("gross_sales", 0.0)
        row["ads_other"] = row.get("ads_cost", 0.0)
        row["backoffice_direct"] = row.get("backoffice_fee", 0.0)
        row["packaging_calculated"] = row.get("packaging_cost", 0.0)
        row["logistics_order_cost_direct"] = row.get("shipping_return_fee", 0.0)
        row["cogs_split_available"] = True
        out.append(base.clean(row))
    return sorted(out, key=lambda r: str(r.get("report_date")))


def _fetch_marketplace_status_audit(base, cur, start: dt.date, end: dt.date) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    specs = {
        "SHOPEE": ("shopee", "orders", "status", SHOPEE_CANCELLED_STATUS_EXACT),
        "TIKTOK": ("tiktok", "orders_pnl", "order_status", TIKTOK_CANCELLED_STATUS_EXACT),
    }
    for channel, (schema, table, status_col, target) in specs.items():
        if not base.relation_exists(cur, schema, table):
            result[channel] = {"available": False, "status_rows": [], "flag_mismatch_count": 0}
            continue
        pred = sql_marketplace_cancelled(f"o.{status_col}")
        rows = base.fetch_all(
            cur,
            f"""
            SELECT
                COALESCE(NULLIF(btrim(o.{status_col}),''), '<BLANK>') AS raw_status,
                ({pred}) AS classified_cancelled,
                COALESCE(o.is_cancelled,false) AS stored_is_cancelled,
                COUNT(*)::numeric AS orders
            FROM {schema}.{table} o
            WHERE o.order_date BETWEEN %s AND %s
            GROUP BY 1,2,3
            ORDER BY classified_cancelled DESC, orders DESC, raw_status
            """,
            (start, end),
        )
        mismatch = base.fetch_all(
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
            "rule": f"normalized exact raw status == {target!r}",
            "cancelled_status_exact": "Đã hủy" if channel == "SHOPEE" else "Canceled",
            "return_refund_classification": "DISABLED_AS_CANCELLED",
            "status_rows": rows,
            "flag_mismatch_count": base.safe_int(mismatch[0].get("mismatch_count")) if mismatch else 0,
        }
    return base.clean(result)


def _fetch_nhanh_status_audit(base, cur, start: dt.date, end: dt.date) -> Dict[str, Any]:
    if not base.relation_exists(cur, "nhanh", "orders"):
        return {"available": False, "status_rows": [], "db_flag_mismatch_count": 0}
    cancel = sql_nhanh_cancelled("o.status")
    ret = sql_nhanh_return("o.status")
    failed = sql_nhanh_failed("o.status")
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
        WHERE o.order_date BETWEEN %s AND %s
        GROUP BY 1,2,3,4,5
        ORDER BY orders DESC, raw_status
        """,
        (start, end),
    )
    mismatch = base.fetch_all(
        cur,
        f"""
        SELECT COUNT(*)::numeric AS mismatch_count
        FROM nhanh.orders o
        WHERE o.order_date BETWEEN %s AND %s
          AND COALESCE(o.is_cancelled,false) <> ({cancel})
        """,
        (start, end),
    )
    cancel_like = [
        r for r in rows
        if ("hủy" in str(r.get("raw_status") or "").casefold() or "huy" in str(r.get("raw_status") or "").casefold())
        and not bool(r.get("classified_cancelled"))
    ]
    return base.clean({
        "available": True,
        "cancelled_statuses_exact": list(NHANH_CANCELLED_STATUSES_EXACT),
        "return_statuses_exact_not_cancelled": list(NHANH_RETURN_STATUSES_EXACT),
        "failed_statuses_exact_not_cancelled": list(NHANH_FAILED_STATUSES_EXACT),
        "successful_status_exact_informational": NHANH_SUCCESS_STATUS_EXACT,
        "normalization": "outer trim only; exact text match",
        "report_uses_raw_status_direct": True,
        "status_rows": rows,
        "db_flag_mismatch_count": base.safe_int(mismatch[0].get("mismatch_count")) if mismatch else 0,
        "unmapped_cancel_like_status_rows": cancel_like,
    })


def _compose_pnl_factory(base):
    original = base.compose_pnl

    def compose_pnl_v46(base_row: Mapping[str, Any], detail: Mapping[str, Any], vat_rates: Mapping[str, float]) -> Dict[str, Any]:
        channel = base.normalize_channel(base_row.get("channel"))
        if channel != "NHANH" or not detail:
            return original(base_row, detail, vat_rates)

        b = dict(base_row)
        direct_profit = (
            base.safe_float(detail.get("net_sales"))
            - base.safe_float(detail.get("cogs_total"))
            - base.safe_float(detail.get("shipping_return_fee"))
            - base.safe_float(detail.get("packaging_cost"))
            - base.safe_float(detail.get("ads_cost"))
            - base.safe_float(detail.get("backoffice_fee"))
        )
        b.update({
            "gross_sales": base.safe_float(detail.get("gross_all_orders")),
            "net_sales": base.safe_float(detail.get("net_sales")),
            "cogs": base.safe_float(detail.get("cogs_total")),
            "shipping_return_fee": base.safe_float(detail.get("shipping_return_fee")),
            "ads_cost": base.safe_float(detail.get("ads_cost")),
            "backoffice_fee": base.safe_float(detail.get("backoffice_fee")),
            "packaging_cost": base.safe_float(detail.get("packaging_cost")),
            "orders": base.safe_int(detail.get("order_count_detail")),
            # Compatibility field: for cross-channel equations success_orders means non-cancelled orders.
            "success_orders": base.safe_int(detail.get("non_cancelled_order_count_detail")),
            "cancelled_orders": base.safe_int(detail.get("cancelled_order_count_detail")),
            "legacy_profit": direct_profit,
        })
        source_map = dict(b.get("source_map") or {})
        source_map.update({
            "nhanh_parent_daily": "direct nhanh.orders + nhanh.order_items + nhanh.ads_costs",
            "cancelled_orders": "exact trimmed statuses: " + " | ".join(NHANH_CANCELLED_STATUSES_EXACT),
            "return_statuses": "informational only, not cancelled: " + " | ".join(NHANH_RETURN_STATUSES_EXACT),
            "failed_statuses": "informational only, not cancelled: " + " | ".join(NHANH_FAILED_STATUSES_EXACT),
            "success_orders_compatibility": "non-cancelled orders; use successful_orders_exact for literal status Thành công",
        })
        b["source_map"] = source_map
        out = original(b, detail, vat_rates)
        out.update({
            "non_cancelled_orders": base.safe_int(detail.get("non_cancelled_order_count_detail")),
            "successful_orders_exact": base.safe_int(detail.get("successful_orders_exact")),
            "returned_orders_status": base.safe_int(detail.get("returned_orders_status")),
            "return_in_progress_orders": base.safe_int(detail.get("return_in_progress_orders")),
            "completed_return_orders": base.safe_int(detail.get("completed_return_orders")),
            "failed_orders_status": base.safe_int(detail.get("failed_orders_status")),
            "return_status_rate": base.ratio(detail.get("returned_orders_status"), detail.get("order_count_detail")),
            "failed_status_rate": base.ratio(detail.get("failed_orders_status"), detail.get("order_count_detail")),
            "status_semantics": {
                "cancelled_statuses_exact": list(NHANH_CANCELLED_STATUSES_EXACT),
                "return_statuses_exact_not_cancelled": list(NHANH_RETURN_STATUSES_EXACT),
                "failed_statuses_exact_not_cancelled": list(NHANH_FAILED_STATUSES_EXACT),
                "successful_status_exact": NHANH_SUCCESS_STATUS_EXACT,
            },
        })
        return base.clean(out)

    return compose_pnl_v46


def _recompute_growth_totals(base, rows: List[Dict[str, Any]], suffix: str) -> None:
    # Rows include channel members; caller handles summary object separately.
    return None


def _harmonize_growth_nhanh(base, growth: Dict[str, Any], nhanh_map: Mapping[str, Mapping[str, Any]], report_date: dt.date) -> None:
    if not growth:
        return
    detail_maps = {"NHANH": nhanh_map}
    week_start = report_date - dt.timedelta(days=report_date.weekday())
    prev_week_start = week_start - dt.timedelta(days=7)
    prev_week_end = prev_week_start + (report_date - week_start)
    mstart = base.month_start(report_date)
    pstart = base.month_start(base.add_months(report_date, -1))
    pend = base.add_months(report_date, -1)

    periods = {
        "weekly": (week_start, report_date, prev_week_start, prev_week_end, "wtd"),
        "monthly": (mstart, report_date, pstart, pend, "mtd"),
    }
    for prefix, (cur_start, cur_end, prev_start, prev_end, suffix) in periods.items():
        score_rows = growth.get(f"{prefix}_scorecard_by_channel", [])
        if isinstance(score_rows, list):
            for row in score_rows:
                if base.normalize_channel(row.get("channel")) == "NHANH":
                    base._set_period_row(row, base._period_direct(detail_maps, "NHANH", cur_start, cur_end), suffix)
            summary = growth.get(f"{prefix}_summary")
            if isinstance(summary, dict) and score_rows:
                for metric in ("gross_sales", "orders", "success_orders", "cancelled_orders"):
                    key = f"{metric}_{suffix}"
                    if key in summary:
                        summary[key] = sum(base.safe_float(r.get(key)) for r in score_rows)
                rate_key = f"cancellation_rate_{suffix}"
                if rate_key in summary:
                    summary[rate_key] = base.ratio(summary.get(f"cancelled_orders_{suffix}"), summary.get(f"orders_{suffix}"))

        comp_rows = growth.get(f"{prefix}_comparison_by_channel", [])
        if isinstance(comp_rows, list):
            for row in comp_rows:
                if base.normalize_channel(row.get("channel")) != "NHANH":
                    continue
                cur_stats = base._period_direct(detail_maps, "NHANH", cur_start, cur_end)
                prev_stats = base._period_direct(detail_maps, "NHANH", prev_start, prev_end)
                base._set_period_row(row, cur_stats, suffix)
                for metric, stat_key in (("gross_sales", "gross"), ("orders", "orders"), ("success_orders", "success"), ("cancelled_orders", "cancelled")):
                    key = f"{metric}_prev_{suffix}"
                    if key in row:
                        row[key] = prev_stats[stat_key]
                rate_key = f"cancellation_rate_prev_{suffix}"
                if rate_key in row:
                    row[rate_key] = base.ratio(prev_stats["cancelled"], prev_stats["orders"])
                base._recalc_delta(row, f"gross_sales_{suffix}", f"gross_sales_prev_{suffix}", "gross_sales_delta", "gross_sales_delta_pct")
                base._recalc_delta(row, f"orders_{suffix}", f"orders_prev_{suffix}", "orders_delta", "orders_delta_pct")

            total_key = f"{prefix}_comparison_vs_previous_" + ("week_same_days" if prefix == "weekly" else "month_same_period")
            total_comp = growth.get(total_key)
            if isinstance(total_comp, dict) and comp_rows:
                for metric in ("gross_sales", "orders", "success_orders", "cancelled_orders"):
                    ck = f"{metric}_{suffix}"; pk = f"{metric}_prev_{suffix}"
                    if ck in total_comp:
                        total_comp[ck] = sum(base.safe_float(r.get(ck)) for r in comp_rows)
                    if pk in total_comp:
                        total_comp[pk] = sum(base.safe_float(r.get(pk)) for r in comp_rows)
                cr=f"cancellation_rate_{suffix}"; pr=f"cancellation_rate_prev_{suffix}"
                if cr in total_comp:
                    total_comp[cr] = base.ratio(total_comp.get(f"cancelled_orders_{suffix}"), total_comp.get(f"orders_{suffix}"))
                if pr in total_comp:
                    total_comp[pr] = base.ratio(total_comp.get(f"cancelled_orders_prev_{suffix}"), total_comp.get(f"orders_prev_{suffix}"))
                base._recalc_delta(total_comp, f"gross_sales_{suffix}", f"gross_sales_prev_{suffix}", "gross_sales_delta", "gross_sales_delta_pct")
                base._recalc_delta(total_comp, f"orders_{suffix}", f"orders_prev_{suffix}", "orders_delta", "orders_delta_pct")

    for block_name in ("weekly_daily_trend", "previous_weekly_daily_trend", "monthly_daily_trend", "previous_monthly_daily_trend"):
        rows = growth.get(block_name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if base.normalize_channel(row.get("channel")) == "NHANH" and row.get("trend_date"):
                d = base._detail_day(detail_maps, "NHANH", base.parse_date(str(row["trend_date"])))
                row["gross_sales"] = d["gross"]
                row["orders"] = d["orders"]
                row["success_orders"] = d["success"]
                row["cancelled_orders"] = d["cancelled"]
                if "cancellation_rate" in row:
                    row["cancellation_rate"] = base.ratio(d["cancelled"], d["orders"])
        # Recompute TOTAL after the Nhanh override.
        by_date: Dict[str, List[Dict[str, Any]]] = {}
        totals: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            key = str(row.get("trend_date"))
            if base.normalize_channel(row.get("channel")) == "TOTAL": totals[key] = row
            else: by_date.setdefault(key, []).append(row)
        for key, total in totals.items():
            members = by_date.get(key, [])
            for metric in ("gross_sales", "orders", "success_orders", "cancelled_orders"):
                if metric in total:
                    total[metric] = sum(base.safe_float(r.get(metric)) for r in members)
            if "cancellation_rate" in total:
                total["cancellation_rate"] = base.ratio(total.get("cancelled_orders"), total.get("orders"))

    for block_name in ("weekly_channel_sales_trend_current_vs_previous", "monthly_channel_sales_trend_current_vs_previous"):
        rows = growth.get(block_name, [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if base.normalize_channel(row.get("channel")) != "NHANH":
                continue
            for side in ("current", "previous"):
                dkey = f"{side}_date"
                if row.get(dkey):
                    d = base._detail_day(detail_maps, "NHANH", base.parse_date(str(row[dkey])))
                    gkey=f"{side}_gross_sales"; okey=f"{side}_orders"
                    if gkey in row: row[gkey]=d["gross"]
                    if okey in row: row[okey]=d["orders"]

    for prefix in ("weekly", "monthly"):
        channel_rows = growth.get(f"{prefix}_channel_sales_trend_current_vs_previous", [])
        total_rows = growth.get(f"{prefix}_total_sales_trend_current_vs_previous", [])
        if not isinstance(channel_rows, list) or not isinstance(total_rows, list):
            continue
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for row in channel_rows:
            grouped.setdefault(base.safe_int(row.get("day_no")), []).append(row)
        for row in total_rows:
            peers = grouped.get(base.safe_int(row.get("day_no")), [])
            for side in ("current", "previous"):
                gkey=f"{side}_gross_sales"; okey=f"{side}_orders"
                if gkey in row: row[gkey]=sum(base.safe_float(r.get(gkey)) for r in peers)
                if okey in row: row[okey]=sum(base.safe_float(r.get(okey)) for r in peers)

    for row in growth.get("monthly_channel_sales_trend_chart", []) if isinstance(growth.get("monthly_channel_sales_trend_chart"), list) else []:
        if base.normalize_channel(row.get("channel")) == "NHANH" and row.get("trend_date"):
            row["gross_sales"] = base._detail_day(detail_maps, "NHANH", base.parse_date(str(row["trend_date"]))) ["gross"]
    for row in growth.get("monthly_channel_sales_share_chart", []) if isinstance(growth.get("monthly_channel_sales_share_chart"), list) else []:
        if base.normalize_channel(row.get("channel")) == "NHANH" and "gross_sales_mtd" in row:
            row["gross_sales_mtd"] = base._period_direct(detail_maps, "NHANH", mstart, report_date)["gross"]
    chart = growth.get("chart_data")
    if isinstance(chart, dict):
        for row in chart.get("monthly_channel_sales_lines", []) if isinstance(chart.get("monthly_channel_sales_lines"), list) else []:
            if base.normalize_channel(row.get("channel")) == "NHANH" and row.get("date") and "gross_sales" in row:
                row["gross_sales"] = base._detail_day(detail_maps, "NHANH", base.parse_date(str(row["date"]))) ["gross"]


def _install_monkey_patches(base):
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    # Avoid misleading single-literal messages in v4.5 internals. Rendering/DQ
    # metadata is replaced with the explicit by-channel contract after build.
    base.CANCELLED_STATUS_EXACT = "channel-specific exact status"
    base.sql_exact_cancelled = sql_marketplace_cancelled
    base.fetch_marketplace_status_audit = lambda cur, start, end: _fetch_marketplace_status_audit(base, cur, start, end)
    base.fetch_nhanh_cogs_split = lambda cur, start, end: _fetch_nhanh_parent_detail(base, cur, start, end)
    base.fetch_nhanh_leaf_detail = lambda cur, start, end: _fetch_nhanh_leaf_detail(base, cur, start, end)
    base.compose_pnl = _compose_pnl_factory(base)

    original_harmonize = base.harmonize_growth_marketplaces
    def harmonize_all(growth, detail_maps, report_date):
        original_harmonize(growth, detail_maps, report_date)
        pstart = base.month_start(base.add_months(report_date, -1))
        with base.connect() as conn:
            with conn.cursor(cursor_factory=base.psycopg2.extras.RealDictCursor) as cur:
                rows = _fetch_nhanh_parent_detail(base, cur, pstart, report_date)
        nhanh_map = base.lookup(rows)
        _harmonize_growth_nhanh(base, growth, nhanh_map, report_date)
    base.harmonize_growth_marketplaces = harmonize_all


def _status_contract() -> Dict[str, Any]:
    return {
        "SHOPEE": {
            "cancelled_statuses_exact": ["Đã hủy"],
            "normalization": "Unicode/case-insensitive + trim/collapse whitespace; exact phrase only",
            "returns_refunds": "NOT_CANCELLED",
        },
        "TIKTOK": {
            "cancelled_statuses_exact": ["Canceled"],
            "normalization": "Unicode/case-insensitive + trim/collapse whitespace; exact phrase only",
            "returns_refunds": "NOT_CANCELLED",
        },
        "NHANH": {
            "cancelled_statuses_exact": list(NHANH_CANCELLED_STATUSES_EXACT),
            "normalization": "outer trim only; exact source text",
            "return_statuses_exact_not_cancelled": list(NHANH_RETURN_STATUSES_EXACT),
            "failed_statuses_exact_not_cancelled": list(NHANH_FAILED_STATUSES_EXACT),
            "successful_status_exact_informational": NHANH_SUCCESS_STATUS_EXACT,
            "success_orders_compatibility": "success_orders means non-cancelled for cross-channel reconciliation; successful_orders_exact is literal Thành công",
        },
    }


def build_package_v46(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    include_growth_context: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    base = _load_base()
    _install_monkey_patches(base)
    package = base.build_package(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        include_growth_context=include_growth_context,
    )
    report_date = base.parse_date(report_date_str)
    query_start = min(base.month_start(report_date), base.month_start(base.add_months(report_date, -1)), base.resolve_compare_date(report_date, compare_mode, explicit_compare_date))

    with base.connect() as conn:
        with conn.cursor(cursor_factory=base.psycopg2.extras.RealDictCursor) as cur:
            nhanh_audit = _fetch_nhanh_status_audit(base, cur, query_start, report_date)

    meta = package.setdefault("metadata", {})
    meta.update({
        "package_version": PACKAGE_VERSION,
        "package_version_int": PACKAGE_VERSION_INT,
        "report_title": "CEO Daily P&L & Growth Package — Channel-Specific Cancel + Nhanh Parent/Leaf + Sold/Gift COGS",
        "scope": "Shopee exact Đã hủy; TikTok exact Canceled; Nhanh exact cancel set separated from return/failure statuses; Nhanh direct parent daily plus leaf; Sold/Gift COGS; Gross→Net Sales→GM1→CM1→CM2→Profit.",
    })

    dq = package.setdefault("data_quality", {})
    dq["status_rule_by_channel"] = _status_contract()
    dq.pop("marketplace_status_rule", None)
    dq["nhanh_parent_daily_source"] = "DIRECT_NHANH_ORDERS_ITEMS_ADS"
    dq["nhanh_db_is_cancelled_used_for_report"] = False
    package["nhanh_status_audit"] = nhanh_audit

    # Guarantee numeric zero-fill for the new Nhanh parent status fields even on
    # days where the base builder had to synthesize a no-sales row.
    nhanh_extra_zero_fields = (
        "non_cancelled_orders", "successful_orders_exact", "returned_orders_status",
        "return_in_progress_orders", "completed_return_orders", "failed_orders_status",
        "return_status_rate", "failed_status_rate",
    )
    nhanh_rows = (package.get("mtd_daily_detail_by_channel") or {}).get("NHANH", [])
    for row in nhanh_rows if isinstance(nhanh_rows, list) else []:
        for field in nhanh_extra_zero_fields:
            if row.get(field) is None:
                row[field] = 0.0
    nhanh_daily = (package.get("daily_pnl_by_channel_map") or {}).get("NHANH")
    if isinstance(nhanh_daily, dict):
        for side in ("current", "previous"):
            row = nhanh_daily.get(side)
            if isinstance(row, dict):
                for field in nhanh_extra_zero_fields:
                    if row.get(field) is None:
                        row[field] = 0.0

    warnings = list(dq.get("warnings") or [])
    if nhanh_audit.get("db_flag_mismatch_count"):
        warnings.append(
            f"NHANH has {int(nhanh_audit['db_flag_mismatch_count'])} legacy DB is_cancelled flags that differ from v4.6 exact cancel semantics. "
            "v4.6 report remains correct because Nhanh cancellation is recomputed directly from raw status; re-import Nhanh when convenient to clean the stored flag."
        )
    if nhanh_audit.get("unmapped_cancel_like_status_rows"):
        warnings.append(
            "NHANH has cancel-like raw status text outside the approved exact whitelist. It is NOT classified as cancelled; review nhanh_status_audit.unmapped_cancel_like_status_rows."
        )
    # Remove the old v4.5 single-literal wording if it survived an internal message.
    errors = [
        str(e).replace("exact status 'channel-specific exact status'", "channel-specific exact cancellation status")
        for e in (dq.get("errors") or [])
    ]
    dq["errors"] = errors
    dq["warnings"] = list(dict.fromkeys(warnings))
    dq["status"] = "FAIL" if errors else ("WARNING" if dq["warnings"] else "PASS")
    dq["can_send"] = not bool(errors)

    package["status_rule_by_channel"] = _status_contract()
    package["nhanh_parent_daily_detail"] = list((package.get("mtd_daily_detail_by_channel") or {}).get("NHANH", []))

    # Clarify marketplace audit metadata after the v4.5 core has built it.
    audit = package.get("marketplace_status_audit") or {}
    if isinstance(audit.get("SHOPEE"), dict):
        audit["SHOPEE"]["rule"] = 'normalized exact raw status == "Đã hủy"'
        audit["SHOPEE"]["cancelled_status_exact"] = "Đã hủy"
    if isinstance(audit.get("TIKTOK"), dict):
        audit["TIKTOK"]["rule"] = 'normalized exact raw status == "Canceled"'
        audit["TIKTOK"]["cancelled_status_exact"] = "Canceled"

    render = package.setdefault("report_rendering_policy", {})
    render["status_rule_by_channel"] = _status_contract()
    render.pop("marketplace_status_rule", None)
    daily_tables = render.setdefault("daily_detail_tables", {})
    nhanh_cfg = daily_tables.setdefault("03_Nhanh", {})
    nhanh_cfg.update({
        "source": "mtd_daily_detail_by_channel.NHANH (alias: nhanh_parent_daily_detail)",
        "parent_daily_source": "DIRECT_NHANH_ORDERS_ITEMS_ADS",
        "columns": [
            "report_date", "gross_sales", "gross_non_cancelled_sales", "gross_cancelled_sales",
            "net_sales", "orders", "non_cancelled_orders", "cancelled_orders", "cancellation_rate",
            "successful_orders_exact", "returned_orders_status", "return_in_progress_orders",
            "completed_return_orders", "failed_orders_status", "sold_cogs", "gift_cogs", "gm1",
            "logistics_order_cost", "packaging_cost", "cm1", "ads_total", "cm2", "backoffice_cost", "profit",
        ],
        "status_display_rule": "Cancelled only uses the exact Nhanh cancel whitelist. Return/failure status counts are informational and must never be added to cancelled_orders.",
        "leaf_channel_source": "nhanh_leaf_channel_map",
        "leaf_daily_source": "nhanh_leaf_daily_detail",
    })

    rules = package.setdefault("pnl_rules", {})
    rules["cancelled_status"] = "Channel-specific exact status only: Shopee=Đã hủy; TikTok=Canceled; Nhanh=Đã hủy/Hệ Thống hủy/Khách hủy/HVC hủy."
    rules["nhanh_returns"] = "Đang hoàn/Xác nhận hoàn/Đã hoàn are return-status audit buckets, not cancelled_orders. Existing +25,000 shipping rule remains only for exact Đã hoàn."
    rules["nhanh_failed"] = "Thất bại is informational failed status, not cancelled_orders. No new P&L treatment is invented in v4.6."
    rules["nhanh_parent_daily"] = "Parent NHANH daily P&L is direct from nhanh.orders/order_items/ads_costs and reconciles to the seven exact leaf labels plus hidden _UNALLOCATED cost bucket."

    return base, base.clean(package)


def self_test() -> int:
    failures: List[str] = []
    if "= 'đã hủy'" not in sql_marketplace_cancelled("o.status"):
        failures.append("Shopee predicate is not exact Đã hủy")
    if "= 'canceled'" not in sql_marketplace_cancelled("o.order_status"):
        failures.append("TikTok predicate is not exact Canceled")
    if "HVC hủy" not in sql_nhanh_cancelled("o.status"):
        failures.append("Nhanh HVC hủy missing from cancel set")
    if set(NHANH_CANCELLED_STATUSES_EXACT) & set(NHANH_RETURN_STATUSES_EXACT):
        failures.append("Nhanh cancel and return status sets overlap")
    if set(NHANH_CANCELLED_STATUSES_EXACT) & set(NHANH_FAILED_STATUSES_EXACT):
        failures.append("Nhanh cancel and failure status sets overlap")
    expected_cancel = {"Đã hủy", "Hệ Thống hủy", "Khách hủy", "HVC hủy"}
    if set(NHANH_CANCELLED_STATUSES_EXACT) != expected_cancel:
        failures.append("Nhanh exact cancel set changed unexpectedly")
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for x in failures:
            print("-", x, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print(json.dumps(_status_contract(), ensure_ascii=False, indent=2))
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

    base = _load_base()
    try:
        report_date = base.parse_date(args.date).isoformat()
        if args.compare_date:
            base.parse_date(args.compare_date)
        vat_rates = base.parse_vat_rates(args.vat_rates)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    if args.require_vat_rates:
        missing = [ch for ch in base.DAILY_PNL_SHEETS if ch not in vat_rates]
        if missing:
            print("ERROR: missing VAT rates for " + ", ".join(missing), file=sys.stderr)
            return 2

    base, package = build_package_v46(
        report_date,
        args.compare_mode,
        args.compare_date,
        vat_rates,
        include_growth_context=not args.without_growth_context,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ceo_daily_pnl_package_v4_6_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_6_{report_date}.md"
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
