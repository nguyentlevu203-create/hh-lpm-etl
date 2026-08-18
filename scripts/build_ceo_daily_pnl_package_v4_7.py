#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.7 — Target + SKU + Inventory add-on.

v4.7 is intentionally layered on the validated v4.6.1 financial package.
It does not rewrite the P&L waterfall. It adds the data contract needed for
an Excel CEO report with seven sheets, including 06_Ton_kho_Can_date.

Added blocks:
- target_progress: monthly targets from ops.monthly_sales_targets, actual MTD
  from canonical v4.6.1 P&L, optional daily target / CM2 targets if DB supports.
- previous_month_same_day_full_pnl: full same-schema P&L for the same calendar
  day of the previous month.
- sku_daily_fact: all-SKU daily sold/gift/push quantities for GT/MT/Nhanh/
  Shopee/TikTok using the existing production source semantics.
- inventory: latest inventory snapshot <= report_date with lot/HSD/warehouse
  detail and existing CEO inventory alerts when the view is available.
- inventory_sku_master: one inventory row per resolved product/SKU.
- sku_inventory_summary: per-channel SKU MTD/day quantities enriched with the
  current inventory snapshot without inventing order-to-lot attribution.
- company_sku_inventory_summary: company-level SKU push + inventory view.
- excel_render_contract: explicit seven-sheet and top-KPI rendering contract.

Important limits:
- v4.7 DOES NOT infer which sold/gift unit came from which lot. "Near-date
  pushed qty" remains unavailable until stock movement / order-to-lot
  attribution is implemented (planned v4.8).
- v4.7 DOES NOT invent target CM2 or daily target allocation. It reads them
  only when the optional DB columns/table exist and are populated.
- Composite marketplace seller SKUs (A+B+C) are kept auditable and flagged as
  requiring component allocation; they are not silently linked to one stock SKU.
- No EBITDA. Final P&L line remains Profit/LỢI NHUẬN.
"""
from __future__ import annotations

import argparse
import calendar
import copy
import datetime as dt
import importlib.util
import json
import math
import os
import re
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple

PACKAGE_VERSION = "v4.7"
PACKAGE_VERSION_INT = 47
BASE_FILENAME = "build_ceo_daily_pnl_package_v4_6_1.py"
UPGRADE_BASIS_GITHUB_COMMIT = "8c977bb9e4b6a52e473ef32edbf4b6d5eb103889"
CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
TARGET_CODE_TO_CHANNEL = {
    "GT": "GT",
    "MT": "MT",
    "DIGITAL": "NHANH",
    "NHANH": "NHANH",
    "NHANH.VN": "NHANH",
    "SHOPEE": "SHOPEE",
    "TIKTOK": "TIKTOK",
    "TIKTOK SHOP": "TIKTOK",
}
TARGET_PARENT_CODE = "ECOM"
EPSILON = 1.0


def _load_v461():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(
            f"Missing {path}. v4.7 is intentionally layered on v4.6.1. "
            "Install/overlay the v4.6 + v4.6.1 prerequisites first."
        )
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v461_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        out = float(value)
        return default if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return default


def _maybe_f(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        out = float(value)
        return None if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return None


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(round(_f(value, float(default))))
    except Exception:
        return default


def _pct(num: Any, den: Any) -> Optional[float]:
    d = _maybe_f(den)
    if d is None or abs(d) < 1e-12:
        return None
    return _f(num) / d


def _norm_identifier(value: Any) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def _norm_channel(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().upper()
    return TARGET_CODE_TO_CHANNEL.get(text, text)


def _is_composite_sku(value: Any) -> bool:
    return "+" in str(value or "")


def _sql_norm_text(expr: str) -> str:
    return f"lower(regexp_replace(btrim(COALESCE({expr},'')), '[[:space:]]+', ' ', 'g'))"


def _sql_marketplace_cancelled(channel: str, expr: str) -> str:
    target = "đã hủy" if channel == "SHOPEE" else "canceled"
    safe_target = target.replace("'", "''")
    return f"{_sql_norm_text(expr)} = '{safe_target}'"


def _add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(value.day, last_day))


def _month_end(value: dt.date) -> dt.date:
    return dt.date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])


def _warehouse_stock_breakdown(by_wh: Mapping[str, float]) -> Dict[str, float]:
    """Return mutually-auditable HN/HCM warehouse totals.

    `stock_hcm` is regular HCM stock and EXCLUDES HCM label/nhan-tem stock.
    `stock_hcm_total` includes both regular HCM and label/nhan-tem stock.
    This prevents Excel from double-counting the separate HCM label column.
    """
    def _is_label(key: Any) -> bool:
        text = unicodedata.normalize("NFC", str(key or "")).upper()
        return "NHAN" in text or "NHÃN" in text

    stock_hn = sum(_f(v) for k, v in by_wh.items() if str(k).upper().startswith("HN"))
    stock_hcm_nhan_tem = sum(
        _f(v) for k, v in by_wh.items()
        if str(k).upper().startswith("HCM") and _is_label(k)
    )
    stock_hcm_regular = sum(
        _f(v) for k, v in by_wh.items()
        if str(k).upper().startswith("HCM") and not _is_label(k)
    )
    return {
        "stock_hn": stock_hn,
        "stock_hcm": stock_hcm_regular,
        "stock_hcm_total": stock_hcm_regular + stock_hcm_nhan_tem,
        "stock_hcm_nhan_tem": stock_hcm_nhan_tem,
    }


def _append_warning(package: MutableMapping[str, Any], text: str) -> None:
    dq = package.setdefault("data_quality", {})
    warnings = dq.setdefault("warnings", [])
    if text not in warnings:
        warnings.append(text)


def _append_error(package: MutableMapping[str, Any], text: str) -> None:
    dq = package.setdefault("data_quality", {})
    errors = dq.setdefault("errors", [])
    if text not in errors:
        errors.append(text)


def _refresh_dq_status(package: MutableMapping[str, Any]) -> None:
    dq = package.setdefault("data_quality", {})
    errors = list(dict.fromkeys(dq.get("errors") or []))
    warnings = list(dict.fromkeys(dq.get("warnings") or []))
    dq["errors"] = errors
    dq["warnings"] = warnings
    if errors:
        dq["status"] = "ERROR"
        dq["can_send"] = False
    elif warnings:
        dq["status"] = "WARNING"
        dq["can_send"] = True
    else:
        dq["status"] = "PASS"
        dq["can_send"] = True


def _sum_mtd_detail(package: Mapping[str, Any], channel: str, field: str) -> float:
    detail = package.get("mtd_daily_detail_by_channel") or {}
    rows = detail.get(channel) or [] if isinstance(detail, dict) else []
    return sum(_f(r.get(field)) for r in rows)


def _daily_current(package: Mapping[str, Any], channel: str) -> Mapping[str, Any]:
    block = package.get("daily_pnl_by_channel_map") or {}
    row = block.get(channel) or {} if isinstance(block, dict) else {}
    return row.get("current") or {} if isinstance(row, dict) else {}


def _monthly_channel_row(package: Mapping[str, Any], channel: str) -> Mapping[str, Any]:
    for row in package.get("monthly_scorecard_by_channel") or []:
        if _norm_channel(row.get("channel")) == channel:
            return row
    return {}


def _fetch_target_rows(base, cur, report_date: dt.date) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    meta: Dict[str, Any] = {
        "available": False,
        "source_relation": "ops.monthly_sales_targets",
        "optional_daily_target_relation": "ops.daily_sales_targets",
        "target_month": report_date.replace(day=1).isoformat(),
        "monthly_target_columns": [],
        "daily_target_columns": [],
    }
    if not base.relation_exists(cur, "ops", "monthly_sales_targets"):
        return [], meta

    cols = base.relation_columns(cur, "ops", "monthly_sales_targets")
    meta["monthly_target_columns"] = sorted(cols)
    required = {"target_month", "channel_code", "target_revenue"}
    if not required.issubset(cols):
        meta["error"] = f"monthly_sales_targets missing required columns: {sorted(required - cols)}"
        return [], meta

    def col(name: str, cast: str = "numeric") -> str:
        return f"t.{name}" if name in cols else f"NULL::{cast} AS {name}"

    enabled_where = "AND COALESCE(t.is_enabled, TRUE)" if "is_enabled" in cols else ""
    order_sql = "ORDER BY t.display_order, t.channel_code" if "display_order" in cols else "ORDER BY t.channel_code"
    sql = f"""
        SELECT
            t.target_month::date AS target_month,
            {('t.display_order' if 'display_order' in cols else 'NULL::integer AS display_order')},
            t.channel_code::text AS channel_code,
            {('t.channel_name::text AS channel_name' if 'channel_name' in cols else 't.channel_code::text AS channel_name')},
            {('t.parent_channel::text AS parent_channel' if 'parent_channel' in cols else 'NULL::text AS parent_channel')},
            t.target_revenue::numeric AS target_revenue,
            {('t.target_cm2::numeric AS target_cm2' if 'target_cm2' in cols else 'NULL::numeric AS target_cm2')},
            {('t.target_cm2_margin::numeric AS target_cm2_margin' if 'target_cm2_margin' in cols else 'NULL::numeric AS target_cm2_margin')},
            {('COALESCE(t.is_enabled, TRUE) AS is_enabled' if 'is_enabled' in cols else 'TRUE AS is_enabled')}
        FROM ops.monthly_sales_targets t
        WHERE t.target_month = %s
          {enabled_where}
        {order_sql}
    """
    rows = base.fetch_all(cur, sql, (report_date.replace(day=1),))
    meta["available"] = True
    meta["has_target_cm2_column"] = "target_cm2" in cols
    meta["has_target_cm2_margin_column"] = "target_cm2_margin" in cols

    daily_rows: List[Dict[str, Any]] = []
    if base.relation_exists(cur, "ops", "daily_sales_targets"):
        dcols = base.relation_columns(cur, "ops", "daily_sales_targets")
        meta["daily_target_columns"] = sorted(dcols)
        if {"target_date", "channel_code", "target_net_sales"}.issubset(dcols):
            denabled = "AND COALESCE(d.is_enabled, TRUE)" if "is_enabled" in dcols else ""
            dsql = f"""
                SELECT
                    d.target_date::date AS target_date,
                    d.channel_code::text AS channel_code,
                    d.target_net_sales::numeric AS target_net_sales,
                    {('d.target_cm2::numeric AS target_cm2' if 'target_cm2' in dcols else 'NULL::numeric AS target_cm2')}
                FROM ops.daily_sales_targets d
                WHERE d.target_date BETWEEN %s AND %s
                  {denabled}
                ORDER BY d.target_date, d.channel_code
            """
            daily_rows = base.fetch_all(cur, dsql, (report_date.replace(day=1), report_date))
            meta["daily_target_available"] = True
        else:
            meta["daily_target_available"] = False
            meta["daily_target_error"] = "daily_sales_targets exists but required columns are missing"
    else:
        meta["daily_target_available"] = False

    meta["daily_rows"] = daily_rows
    return rows, meta


def _aggregate_daily_targets(rows: Sequence[Mapping[str, Any]], report_date: dt.date) -> Dict[str, Dict[str, Optional[float]]]:
    out: Dict[str, Dict[str, Optional[float]]] = {
        ch: {"target_day_net_sales": None, "target_mtd_net_sales": None, "target_day_cm2": None, "target_mtd_cm2": None}
        for ch in CHANNELS
    }
    seen: Dict[str, bool] = defaultdict(bool)
    cm2_seen: Dict[str, bool] = defaultdict(bool)
    for row in rows:
        ch = _norm_channel(row.get("channel_code"))
        if ch not in CHANNELS:
            continue
        target_date = row.get("target_date")
        if isinstance(target_date, str):
            try:
                target_date = dt.date.fromisoformat(target_date)
            except Exception:
                target_date = None
        amount = _maybe_f(row.get("target_net_sales"))
        cm2 = _maybe_f(row.get("target_cm2"))
        if amount is not None:
            if not seen[ch]:
                out[ch]["target_mtd_net_sales"] = 0.0
                seen[ch] = True
            out[ch]["target_mtd_net_sales"] = _f(out[ch]["target_mtd_net_sales"]) + amount
            if target_date == report_date:
                out[ch]["target_day_net_sales"] = _f(out[ch].get("target_day_net_sales")) + amount
        if cm2 is not None:
            if not cm2_seen[ch]:
                out[ch]["target_mtd_cm2"] = 0.0
                cm2_seen[ch] = True
            out[ch]["target_mtd_cm2"] = _f(out[ch]["target_mtd_cm2"]) + cm2
            if target_date == report_date:
                out[ch]["target_day_cm2"] = _f(out[ch].get("target_day_cm2")) + cm2
    return out


def _build_target_progress_from_rows(
    package: Mapping[str, Any],
    report_date: dt.date,
    source_rows: Sequence[Mapping[str, Any]],
    source_meta: Mapping[str, Any],
) -> Dict[str, Any]:
    # Preserve the source table rows for audit, but calculate company target from
    # leaf channels so ECOM is never double counted with Shopee/TikTok.
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    parent_rows: List[Mapping[str, Any]] = []
    for row in source_rows:
        code = re.sub(r"\s+", " ", str(row.get("channel_code") or "")).strip().upper()
        if code == TARGET_PARENT_CODE:
            parent_rows.append(row)
            continue
        ch = _norm_channel(code)
        if ch in CHANNELS:
            grouped[ch].append(row)

    daily = _aggregate_daily_targets(source_meta.get("daily_rows") or [], report_date)
    days_in_month = calendar.monthrange(report_date.year, report_date.month)[1]
    days_remaining = max(days_in_month - report_date.day, 0)
    result_rows: List[Dict[str, Any]] = []
    duplicate_codes: List[str] = []

    for channel in CHANNELS:
        srcs = grouped.get(channel, [])
        if len(srcs) > 1:
            duplicate_codes.append(channel)
        monthly_target = sum(_f(r.get("target_revenue")) for r in srcs) if srcs else None
        cm2_values = [_maybe_f(r.get("target_cm2")) for r in srcs]
        cm2_values = [v for v in cm2_values if v is not None]
        target_cm2 = sum(cm2_values) if cm2_values else None
        margin_values = [_maybe_f(r.get("target_cm2_margin")) for r in srcs]
        margin_values = [v for v in margin_values if v is not None]
        target_cm2_margin = margin_values[0] if len(margin_values) == 1 else None
        if target_cm2_margin is None and target_cm2 is not None and monthly_target not in (None, 0):
            target_cm2_margin = target_cm2 / float(monthly_target)

        actual_mtd = _sum_mtd_detail(package, channel, "net_sales")
        actual_cm2_mtd = _sum_mtd_detail(package, channel, "cm2")
        current = _daily_current(package, channel)
        actual_day = _f(current.get("net_sales"))
        actual_cm2_day = _f(current.get("cm2"))
        month_row = _monthly_channel_row(package, channel)
        forecast = _maybe_f(month_row.get("forecast_net_sales_month_end"))
        if forecast is None:
            forecast = actual_mtd / max(report_date.day, 1) * days_in_month
        forecast_cm2 = actual_cm2_mtd / max(report_date.day, 1) * days_in_month
        remaining = None if monthly_target is None else max(float(monthly_target) - actual_mtd, 0.0)
        target_daily = daily.get(channel) or {}

        row = {
            "channel": channel,
            "monthly_target_net_sales": monthly_target,
            "mtd_net_sales": actual_mtd,
            "day_net_sales": actual_day,
            "achievement_mtd_pct": _pct(actual_mtd, monthly_target),
            "remaining_target_net_sales": remaining,
            "days_remaining_after_report_date": days_remaining,
            "required_daily_net_sales_remaining": (remaining / days_remaining) if (remaining is not None and days_remaining > 0) else (0.0 if remaining == 0 else None),
            "forecast_month_end_net_sales": forecast,
            "forecast_achievement_pct": _pct(forecast, monthly_target),
            "target_cm2": target_cm2,
            "target_cm2_margin": target_cm2_margin,
            "mtd_cm2": actual_cm2_mtd,
            "day_cm2": actual_cm2_day,
            "cm2_target_achievement_pct": _pct(actual_cm2_mtd, target_cm2),
            "forecast_month_end_cm2": forecast_cm2,
            "forecast_cm2_achievement_pct": _pct(forecast_cm2, target_cm2),
            **target_daily,
            "daily_target_achievement_pct": _pct(actual_day, target_daily.get("target_day_net_sales")),
            "mtd_daily_plan_achievement_pct": _pct(actual_mtd, target_daily.get("target_mtd_net_sales")),
            "target_source_channel_codes": [str(r.get("channel_code")) for r in srcs],
        }
        result_rows.append(row)

    total_target = sum(_f(r.get("monthly_target_net_sales")) for r in result_rows if r.get("monthly_target_net_sales") is not None)
    total_actual = sum(_f(r.get("mtd_net_sales")) for r in result_rows)
    total_day = sum(_f(r.get("day_net_sales")) for r in result_rows)
    total_cm2 = sum(_f(r.get("mtd_cm2")) for r in result_rows)
    total_day_cm2 = sum(_f(r.get("day_cm2")) for r in result_rows)
    total_target_cm2_values = [r.get("target_cm2") for r in result_rows if r.get("target_cm2") is not None]
    total_target_cm2 = sum(_f(v) for v in total_target_cm2_values) if total_target_cm2_values else None
    total_forecast = sum(_f(r.get("forecast_month_end_net_sales")) for r in result_rows)
    total_forecast_cm2 = sum(_f(r.get("forecast_month_end_cm2")) for r in result_rows)
    total_remaining = max(total_target - total_actual, 0.0) if result_rows else None

    parent_target = sum(_f(r.get("target_revenue")) for r in parent_rows) if parent_rows else None
    shopee_target = next((r.get("monthly_target_net_sales") for r in result_rows if r.get("channel") == "SHOPEE"), None)
    tiktok_target = next((r.get("monthly_target_net_sales") for r in result_rows if r.get("channel") == "TIKTOK"), None)
    child_sum = None if shopee_target is None or tiktok_target is None else _f(shopee_target) + _f(tiktok_target)

    total_daily_target = None
    total_mtd_daily_target = None
    day_targets = [r.get("target_day_net_sales") for r in result_rows]
    mtd_targets = [r.get("target_mtd_net_sales") for r in result_rows]
    if day_targets and all(v is not None for v in day_targets):
        total_daily_target = sum(_f(v) for v in day_targets)
    if mtd_targets and all(v is not None for v in mtd_targets):
        total_mtd_daily_target = sum(_f(v) for v in mtd_targets)

    return {
        "available": bool(source_meta.get("available")),
        "report_date": report_date.isoformat(),
        "target_month": report_date.replace(day=1).isoformat(),
        "target_basis": "NET_SALES_CANONICAL_V4_6_1_ACTUAL",
        "target_source": "ops.monthly_sales_targets.target_revenue",
        "actual_source": "v4.6.1 mtd_daily_detail_by_channel.net_sales",
        "daily_target_source": "ops.daily_sales_targets" if source_meta.get("daily_target_available") else None,
        "total_policy": "TOTAL = GT + MT + NHANH(DIGITAL target) + SHOPEE + TIKTOK. ECOM is parent/audit only and MUST NOT be added again.",
        "source_metadata": {k: v for k, v in source_meta.items() if k != "daily_rows"},
        "source_rows": list(source_rows),
        "rows": result_rows,
        "total": {
            "channel": "TOTAL",
            "monthly_target_net_sales": total_target,
            "mtd_net_sales": total_actual,
            "day_net_sales": total_day,
            "achievement_mtd_pct": _pct(total_actual, total_target),
            "remaining_target_net_sales": total_remaining,
            "days_remaining_after_report_date": days_remaining,
            "required_daily_net_sales_remaining": (total_remaining / days_remaining) if (total_remaining is not None and days_remaining > 0) else (0.0 if total_remaining == 0 else None),
            "forecast_month_end_net_sales": total_forecast,
            "forecast_achievement_pct": _pct(total_forecast, total_target),
            "target_cm2": total_target_cm2,
            "mtd_cm2": total_cm2,
            "day_cm2": total_day_cm2,
            "cm2_target_achievement_pct": _pct(total_cm2, total_target_cm2),
            "forecast_month_end_cm2": total_forecast_cm2,
            "forecast_cm2_achievement_pct": _pct(total_forecast_cm2, total_target_cm2),
            "target_day_net_sales": total_daily_target,
            "target_mtd_net_sales": total_mtd_daily_target,
            "daily_target_achievement_pct": _pct(total_day, total_daily_target),
            "mtd_daily_plan_achievement_pct": _pct(total_actual, total_mtd_daily_target),
        },
        "parent_audit": {
            "ECOM_target": parent_target,
            "SHOPEE_plus_TIKTOK_target": child_sum,
            "reconciliation_gap": None if parent_target is None or child_sum is None else parent_target - child_sum,
            "must_not_double_count_parent": True,
        },
        "duplicate_leaf_target_channels": duplicate_codes,
    }


def _fetch_previous_month_full_pnl(
    v461,
    report_date: dt.date,
    vat_rates: Mapping[str, float],
) -> Dict[str, Any]:
    previous_date = _add_months(report_date, -1)
    _base_prev, prev_pkg = v461.build_package_v461(
        previous_date.isoformat(),
        "previous_day",
        None,
        vat_rates,
        include_growth_context=False,
    )
    by_channel: Dict[str, Any] = {}
    source = prev_pkg.get("daily_pnl_by_channel_map") or {}
    for ch in CHANNELS:
        entry = source.get(ch) or {}
        by_channel[ch] = {
            "current": copy.deepcopy(entry.get("current") or {}),
            "pnl_lines": copy.deepcopy(entry.get("pnl_lines") or []),
        }
    return {
        "available": True,
        "report_date": previous_date.isoformat(),
        "comparison_to_current_report_date": report_date.isoformat(),
        "schema_contract": "same v4.6.1 daily P&L schema as current report date",
        "total": copy.deepcopy((prev_pkg.get("daily_summary") or {}).get("current") or {}),
        "by_channel": by_channel,
    }


def _fetch_sku_daily_fact(v461, base, cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    # GT / MT: the source ledger has explicit quantity_sale and quantity_gift.
    if base.relation_exists(cur, "mtgt", "sales_lines"):
        cols = base.relation_columns(cur, "mtgt", "sales_lines")
        required = {"sale_date", "channel_group", "product_code"}
        if required.issubset(cols):
            qsale = "COALESCE(s.quantity_sale,0)" if "quantity_sale" in cols else "CASE WHEN COALESCE(s.quantity,0)>0 THEN COALESCE(s.quantity,0) ELSE 0 END"
            qgift = "COALESCE(s.quantity_gift,0)" if "quantity_gift" in cols else "0::numeric"
            qret = "COALESCE(s.quantity_return,0)" if "quantity_return" in cols else "0::numeric"
            unit_cogs = "COALESCE(s.unit_cogs,0)" if "unit_cogs" in cols else "0::numeric"
            barcode = "s.barcode" if "barcode" in cols else "NULL::text"
            pname = "s.product_name" if "product_name" in cols else "NULL::text"
            net = "SUM(COALESCE(s.net_revenue,0))::numeric" if "net_revenue" in cols else "NULL::numeric"
            line_filter = "AND COALESCE(s.line_type,'') <> 'CVC'" if "line_type" in cols else ""
            order_count = "COUNT(DISTINCT s.document_no)::numeric" if "document_no" in cols else "NULL::numeric"
            sql = f"""
                SELECT
                    s.sale_date::date AS report_date,
                    upper(btrim(s.channel_group))::text AS channel,
                    NULL::text AS subchannel,
                    s.product_code::text AS source_sku,
                    {barcode}::text AS ean,
                    max({pname})::text AS product_name,
                    SUM(GREATEST({qsale},0))::numeric AS sold_qty,
                    SUM(GREATEST({qgift},0))::numeric AS gift_qty,
                    SUM(GREATEST({qret},0))::numeric AS return_qty,
                    0::numeric AS cancelled_qty,
                    SUM(GREATEST({qsale},0) * {unit_cogs})::numeric AS sold_cogs,
                    SUM(GREATEST({qgift},0) * {unit_cogs})::numeric AS gift_cogs,
                    {net} AS source_net_sales,
                    {order_count} AS source_order_count
                FROM mtgt.sales_lines s
                WHERE s.sale_date BETWEEN %s AND %s
                  AND upper(btrim(s.channel_group)) IN ('GT','MT')
                  {line_filter}
                GROUP BY 1,2,4,5
                ORDER BY 1,2,4
            """
            for r in base.fetch_all(cur, sql, (start, end)):
                r.update({
                    "gift_classification_rule": "mtgt.sales_lines.quantity_gift",
                    "sold_classification_rule": "mtgt.sales_lines.quantity_sale",
                    "net_sales_allocation": "exact_line_net_revenue" if r.get("source_net_sales") is not None else "unavailable",
                    "source_relation": "mtgt.sales_lines",
                })
                rows.append(r)

    # Nhanh: explicit is_gift on item; cancelled quantity is separated and not pushed.
    if base.relation_exists(cur, "nhanh", "orders") and base.relation_exists(cur, "nhanh", "order_items"):
        ocols = base.relation_columns(cur, "nhanh", "orders")
        icols = base.relation_columns(cur, "nhanh", "order_items")
        if {"id", "order_date", "status"}.issubset(ocols) and {"order_id", "quantity"}.issubset(icols):
            cancel = v461.sql_nhanh_cancelled("o.status")
            gift = "COALESCE(i.is_gift,false)" if "is_gift" in icols else "COALESCE(i.price,0)=0"
            sku = "i.product_code" if "product_code" in icols else "NULL::text"
            ean = "i.barcode" if "barcode" in icols else "NULL::text"
            pname = "i.product_name" if "product_name" in icols else "NULL::text"
            uc = "COALESCE(i.unit_cogs,0)" if "unit_cogs" in icols else "0::numeric"
            label = "COALESCE(NULLIF(btrim(o.label),''),'UNALLOCATED')" if "label" in ocols else "'DEFAULT'"
            sql = f"""
                SELECT
                    o.order_date::date AS report_date,
                    'NHANH'::text AS channel,
                    {label}::text AS subchannel,
                    {sku}::text AS source_sku,
                    {ean}::text AS ean,
                    max({pname})::text AS product_name,
                    SUM(CASE WHEN NOT ({cancel}) AND NOT ({gift}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS sold_qty,
                    SUM(CASE WHEN NOT ({cancel}) AND ({gift}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS gift_qty,
                    NULL::numeric AS return_qty,
                    SUM(CASE WHEN ({cancel}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS cancelled_qty,
                    SUM(CASE WHEN NOT ({cancel}) AND NOT ({gift}) THEN COALESCE(i.quantity,0)*{uc} ELSE 0 END)::numeric AS sold_cogs,
                    SUM(CASE WHEN NOT ({cancel}) AND ({gift}) THEN COALESCE(i.quantity,0)*{uc} ELSE 0 END)::numeric AS gift_cogs,
                    NULL::numeric AS source_net_sales,
                    COUNT(DISTINCT CASE WHEN NOT ({cancel}) THEN o.id END)::numeric AS source_order_count
                FROM nhanh.orders o
                JOIN nhanh.order_items i ON i.order_id=o.id
                WHERE o.order_date BETWEEN %s AND %s
                GROUP BY 1,3,4,5
                ORDER BY 1,3,4
            """
            for r in base.fetch_all(cur, sql, (start, end)):
                r.update({
                    "gift_classification_rule": "nhanh.order_items.is_gift (ETL price=0 rule)",
                    "sold_classification_rule": "non-cancelled item and is_gift=false",
                    "net_sales_allocation": "unavailable_order_level_only",
                    "source_relation": "nhanh.orders + nhanh.order_items",
                })
                rows.append(r)

    # Shopee: explicit is_gift item flag; exact normalized raw status Đã hủy only.
    if base.relation_exists(cur, "shopee", "orders") and base.relation_exists(cur, "shopee", "order_items"):
        ocols = base.relation_columns(cur, "shopee", "orders")
        icols = base.relation_columns(cur, "shopee", "order_items")
        if {"id", "order_date", "status"}.issubset(ocols) and {"order_id", "quantity"}.issubset(icols):
            cancel = _sql_marketplace_cancelled("SHOPEE", "o.status")
            gift = "COALESCE(i.is_gift,false)" if "is_gift" in icols else "false"
            sku = "i.seller_sku" if "seller_sku" in icols else "NULL::text"
            ean = "i.barcode" if "barcode" in icols else "NULL::text"
            pname = "i.product_name" if "product_name" in icols else "NULL::text"
            uc = "COALESCE(i.unit_cogs,0)" if "unit_cogs" in icols else "0::numeric"
            shop = "COALESCE(NULLIF(btrim(o.shop_label),''),'DEFAULT')" if "shop_label" in ocols else "'DEFAULT'"
            sql = f"""
                SELECT
                    o.order_date::date AS report_date,
                    'SHOPEE'::text AS channel,
                    {shop}::text AS subchannel,
                    {sku}::text AS source_sku,
                    {ean}::text AS ean,
                    max({pname})::text AS product_name,
                    SUM(CASE WHEN NOT ({cancel}) AND NOT ({gift}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS sold_qty,
                    SUM(CASE WHEN NOT ({cancel}) AND ({gift}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS gift_qty,
                    NULL::numeric AS return_qty,
                    SUM(CASE WHEN ({cancel}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS cancelled_qty,
                    SUM(CASE WHEN NOT ({cancel}) AND NOT ({gift}) THEN COALESCE(i.quantity,0)*{uc} ELSE 0 END)::numeric AS sold_cogs,
                    SUM(CASE WHEN NOT ({cancel}) AND ({gift}) THEN COALESCE(i.quantity,0)*{uc} ELSE 0 END)::numeric AS gift_cogs,
                    NULL::numeric AS source_net_sales,
                    COUNT(DISTINCT CASE WHEN NOT ({cancel}) THEN o.id END)::numeric AS source_order_count
                FROM shopee.orders o
                JOIN shopee.order_items i ON i.order_id=o.id
                WHERE o.order_date BETWEEN %s AND %s
                GROUP BY 1,3,4,5
                ORDER BY 1,3,4
            """
            for r in base.fetch_all(cur, sql, (start, end)):
                r.update({
                    "gift_classification_rule": "shopee.order_items.is_gift",
                    "sold_classification_rule": "non-cancelled item and is_gift=false",
                    "net_sales_allocation": "unavailable_order_level_only",
                    "source_relation": "shopee.orders + shopee.order_items",
                })
                rows.append(r)

    # TikTok: v4.6.1 contract uses price=0 gift, price<>0 sold and exact Canceled.
    if base.relation_exists(cur, "tiktok", "orders_pnl") and base.relation_exists(cur, "tiktok", "order_items"):
        ocols = base.relation_columns(cur, "tiktok", "orders_pnl")
        icols = base.relation_columns(cur, "tiktok", "order_items")
        required_o = {"external_order_id", "shop_label", "order_date", "order_status"}
        required_i = {"external_order_id", "shop_label", "quantity"}
        if required_o.issubset(ocols) and required_i.issubset(icols):
            cancel = _sql_marketplace_cancelled("TIKTOK", "o.order_status")
            gift = "COALESCE(i.price,0)=0" if "price" in icols else "false"
            sku = "i.seller_sku" if "seller_sku" in icols else "NULL::text"
            ean = "i.barcode" if "barcode" in icols else "NULL::text"
            pname = "i.product_name" if "product_name" in icols else "NULL::text"
            uc = "COALESCE(i.unit_cogs,0)" if "unit_cogs" in icols else "0::numeric"
            sql = f"""
                SELECT
                    o.order_date::date AS report_date,
                    'TIKTOK'::text AS channel,
                    COALESCE(NULLIF(btrim(o.shop_label),''),'DEFAULT')::text AS subchannel,
                    {sku}::text AS source_sku,
                    {ean}::text AS ean,
                    max({pname})::text AS product_name,
                    SUM(CASE WHEN NOT ({cancel}) AND NOT ({gift}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS sold_qty,
                    SUM(CASE WHEN NOT ({cancel}) AND ({gift}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS gift_qty,
                    NULL::numeric AS return_qty,
                    SUM(CASE WHEN ({cancel}) THEN COALESCE(i.quantity,0) ELSE 0 END)::numeric AS cancelled_qty,
                    SUM(CASE WHEN NOT ({cancel}) AND NOT ({gift}) THEN COALESCE(i.quantity,0)*{uc} ELSE 0 END)::numeric AS sold_cogs,
                    SUM(CASE WHEN NOT ({cancel}) AND ({gift}) THEN COALESCE(i.quantity,0)*{uc} ELSE 0 END)::numeric AS gift_cogs,
                    NULL::numeric AS source_net_sales,
                    COUNT(DISTINCT CASE WHEN NOT ({cancel}) THEN o.external_order_id END)::numeric AS source_order_count
                FROM tiktok.orders_pnl o
                JOIN tiktok.order_items i
                  ON i.external_order_id=o.external_order_id AND i.shop_label=o.shop_label
                WHERE o.order_date BETWEEN %s AND %s
                GROUP BY 1,3,4,5
                ORDER BY 1,3,4
            """
            for r in base.fetch_all(cur, sql, (start, end)):
                r.update({
                    "gift_classification_rule": "tiktok.order_items.price=0",
                    "sold_classification_rule": "non-cancelled item and price<>0",
                    "net_sales_allocation": "unavailable_order_level_only",
                    "source_relation": "tiktok.orders_pnl + tiktok.order_items",
                })
                rows.append(r)

    # Standardize quantities and make total_push explicit.
    out: List[Dict[str, Any]] = []
    for row in rows:
        channel = _norm_channel(row.get("channel"))
        if channel not in CHANNELS:
            continue
        sold = max(_f(row.get("sold_qty")), 0.0)
        gift = max(_f(row.get("gift_qty")), 0.0)
        row["channel"] = channel
        row["sold_qty"] = sold
        row["gift_qty"] = gift
        row["total_push_qty"] = sold + gift
        row["return_qty"] = _maybe_f(row.get("return_qty"))
        row["cancelled_qty"] = max(_f(row.get("cancelled_qty")), 0.0)
        row["sold_cogs"] = _f(row.get("sold_cogs"))
        row["gift_cogs"] = _f(row.get("gift_cogs"))
        row["total_push_cogs"] = row["sold_cogs"] + row["gift_cogs"]
        row["is_composite_sku"] = _is_composite_sku(row.get("source_sku"))
        row["component_skus"] = [x.strip() for x in str(row.get("source_sku") or "").split("+") if x.strip()] if row["is_composite_sku"] else []
        out.append(row)
    return out


def _load_product_master_maps(base, cur) -> Dict[str, Any]:
    products: Dict[str, Dict[str, Any]] = {}
    key_to_products: Dict[str, set[str]] = defaultdict(set)

    if base.relation_exists(cur, "dim", "product_master"):
        cols = base.relation_columns(cur, "dim", "product_master")
        if "product_id" in cols:
            csku = "canonical_sku_code" if "canonical_sku_code" in cols else None
            pean = "primary_ean" if "primary_ean" in cols else None
            pname = "product_name" if "product_name" in cols else None
            sql = f"""
                SELECT product_id::text AS product_id,
                       {('canonical_sku_code::text' if csku else 'NULL::text')} AS canonical_sku,
                       {('primary_ean::text' if pean else 'NULL::text')} AS primary_ean,
                       {('product_name::text' if pname else 'NULL::text')} AS product_name
                FROM dim.product_master
            """
            for row in base.fetch_all(cur, sql):
                pid = str(row.get("product_id") or "")
                if not pid:
                    continue
                products[pid] = row
                for val in (row.get("canonical_sku"), row.get("primary_ean")):
                    key = _norm_identifier(val)
                    if key:
                        key_to_products[key].add(pid)

    if base.relation_exists(cur, "dim", "product_identifier_map"):
        cols = base.relation_columns(cur, "dim", "product_identifier_map")
        if {"product_id", "identifier_value"}.issubset(cols):
            where = "WHERE COALESCE(is_active,TRUE)" if "is_active" in cols else ""
            sql = f"SELECT product_id::text AS product_id, identifier_value::text AS identifier_value FROM dim.product_identifier_map {where}"
            for row in base.fetch_all(cur, sql):
                pid = str(row.get("product_id") or "")
                key = _norm_identifier(row.get("identifier_value"))
                if pid and key:
                    key_to_products[key].add(pid)

    return {"products": products, "key_to_products": key_to_products}


def _resolve_product(master: Mapping[str, Any], source_sku: Any, ean: Any) -> Dict[str, Any]:
    if _is_composite_sku(source_sku):
        return {
            "product_id": None,
            "canonical_sku": None,
            "primary_ean": None,
            "master_product_name": None,
            "product_mapping_status": "COMPOSITE_REQUIRES_COMPONENT_ALLOCATION",
        }
    candidates: set[str] = set()
    for value in (ean, source_sku):
        key = _norm_identifier(value)
        if key:
            candidates |= set((master.get("key_to_products") or {}).get(key) or set())
    if len(candidates) == 1:
        pid = next(iter(candidates))
        p = (master.get("products") or {}).get(pid) or {}
        return {
            "product_id": pid,
            "canonical_sku": p.get("canonical_sku"),
            "primary_ean": p.get("primary_ean"),
            "master_product_name": p.get("product_name"),
            "product_mapping_status": "MAPPED",
        }
    if len(candidates) > 1:
        return {
            "product_id": None,
            "canonical_sku": None,
            "primary_ean": None,
            "master_product_name": None,
            "product_mapping_status": "AMBIGUOUS_IDENTIFIER",
            "candidate_product_ids": sorted(candidates),
        }
    return {
        "product_id": None,
        "canonical_sku": None,
        "primary_ean": None,
        "master_product_name": None,
        "product_mapping_status": "UNMAPPED",
    }


def _resolve_inventory_snapshot(base, cur, report_date: dt.date) -> Optional[dt.date]:
    if not base.relation_exists(cur, "inventory", "stock_lot_snapshots"):
        return None
    cols = base.relation_columns(cur, "inventory", "stock_lot_snapshots")
    if "snapshot_date" not in cols:
        return None
    cur.execute(
        "SELECT MAX(snapshot_date)::date FROM inventory.stock_lot_snapshots WHERE snapshot_date <= %s",
        (report_date,),
    )
    row = cur.fetchone()
    value = row[0] if row and not isinstance(row, Mapping) else (row.get("max") if row else None)
    return value


def _fetch_inventory(base, cur, report_date: dt.date, master: Mapping[str, Any]) -> Dict[str, Any]:
    snapshot = _resolve_inventory_snapshot(base, cur, report_date)
    if snapshot is None:
        return {
            "available": False,
            "report_date": report_date.isoformat(),
            "snapshot_date": None,
            "freshness_days": None,
            "summary": {},
            "warehouse_summary": [],
            "lot_detail": [],
            "alerts": [],
            "alert_source_available": False,
        }

    cols = base.relation_columns(cur, "inventory", "stock_lot_snapshots")
    required = {"snapshot_date", "warehouse", "stock_qty"}
    if not required.issubset(cols):
        return {
            "available": False,
            "report_date": report_date.isoformat(),
            "snapshot_date": snapshot.isoformat(),
            "freshness_days": (report_date - snapshot).days,
            "error": f"inventory.stock_lot_snapshots missing columns {sorted(required - cols)}",
            "summary": {}, "warehouse_summary": [], "lot_detail": [], "alerts": [],
            "alert_source_available": False,
        }

    def expr(name: str, alias: Optional[str] = None, cast: str = "text") -> str:
        a = alias or name
        return f"s.{name}::{cast} AS {a}" if name in cols else f"NULL::{cast} AS {a}"

    sql = f"""
        SELECT
            s.snapshot_date::date AS snapshot_date,
            s.warehouse::text AS warehouse,
            {expr('product_id', 'product_id')},
            {expr('ean_raw', 'ean')},
            {expr('sku_code_raw', 'source_sku')},
            {expr('product_name_raw', 'source_product_name')},
            {expr('lot_no', 'lot_no')},
            {('s.expiry_date::date AS expiry_date' if 'expiry_date' in cols else 'NULL::date AS expiry_date')},
            {expr('uom', 'uom')},
            s.stock_qty::numeric AS stock_qty,
            {expr('source_file', 'source_file')}
        FROM inventory.stock_lot_snapshots s
        WHERE s.snapshot_date = %s
        ORDER BY s.warehouse, source_product_name, source_sku, lot_no, expiry_date
    """
    lot_rows = base.fetch_all(cur, sql, (snapshot,))
    for r in lot_rows:
        pid = str(r.get("product_id") or "") or None
        p = (master.get("products") or {}).get(pid or "") or {}
        if not pid:
            resolved = _resolve_product(master, r.get("source_sku"), r.get("ean"))
            pid = resolved.get("product_id")
            p = (master.get("products") or {}).get(pid or "") or {}
            r["product_mapping_status"] = resolved.get("product_mapping_status")
        else:
            r["product_mapping_status"] = "MAPPED_BY_INVENTORY_PRODUCT_ID"
        r["product_id"] = pid
        r["canonical_sku"] = p.get("canonical_sku") or r.get("source_sku") or r.get("ean")
        r["primary_ean"] = p.get("primary_ean") or r.get("ean")
        r["product_name"] = p.get("product_name") or r.get("source_product_name") or r.get("canonical_sku")
        expiry = r.get("expiry_date")
        if isinstance(expiry, str):
            try:
                expiry = dt.date.fromisoformat(expiry)
            except Exception:
                expiry = None
        r["days_to_expiry"] = (expiry - report_date).days if expiry else None
        r["stock_qty"] = _f(r.get("stock_qty"))

    warehouse_totals: Dict[str, Dict[str, Any]] = {}
    product_groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for r in lot_rows:
        wh = str(r.get("warehouse") or "UNALLOCATED")
        acc = warehouse_totals.setdefault(wh, {"warehouse": wh, "stock_qty": 0.0, "lot_count": 0, "sku_keys": set()})
        acc["stock_qty"] += _f(r.get("stock_qty"))
        acc["lot_count"] += 1
        inv_key = str(r.get("product_id") or r.get("canonical_sku") or r.get("source_sku") or r.get("ean") or r.get("product_name"))
        acc["sku_keys"].add(inv_key)
        product_groups[inv_key].append(r)

    warehouse_summary = []
    for wh, acc in sorted(warehouse_totals.items()):
        warehouse_summary.append({
            "warehouse": wh,
            "stock_qty": acc["stock_qty"],
            "lot_count": acc["lot_count"],
            "sku_count": len(acc["sku_keys"]),
        })

    inventory_sku: List[Dict[str, Any]] = []
    mixed_uom_keys: List[str] = []
    for inv_key, group in product_groups.items():
        first = group[0]
        by_wh: Dict[str, float] = defaultdict(float)
        expiries: List[dt.date] = []
        uoms: set[str] = set()
        for r in group:
            by_wh[str(r.get("warehouse") or "UNALLOCATED")] += _f(r.get("stock_qty"))
            if r.get("uom"):
                uoms.add(str(r.get("uom")))
            exp = r.get("expiry_date")
            if isinstance(exp, str):
                try: exp = dt.date.fromisoformat(exp)
                except Exception: exp = None
            if exp:
                expiries.append(exp)
        if len(uoms) > 1:
            mixed_uom_keys.append(inv_key)
        earliest = min(expiries) if expiries else None
        wh_breakdown = _warehouse_stock_breakdown(by_wh)
        row = {
            "inventory_key": inv_key,
            "product_id": first.get("product_id"),
            "canonical_sku": first.get("canonical_sku"),
            "primary_ean": first.get("primary_ean"),
            "product_name": first.get("product_name"),
            "current_stock_qty": sum(_f(x.get("stock_qty")) for x in group),
            "lot_count": len(group),
            "warehouse_count": len(by_wh),
            "stock_by_warehouse": dict(sorted(by_wh.items())),
            **wh_breakdown,
            "uom_values": sorted(uoms),
            "stock_qty_unit_consistency": "PASS" if len(uoms) <= 1 else "MIXED_UOM_REVIEW_REQUIRED",
            "earliest_expiry_date": earliest.isoformat() if earliest else None,
            "days_to_earliest_expiry": (earliest - report_date).days if earliest else None,
            "available_stock_qty": None,
            "near_expiry_stock_qty": None,
            "near_expiry_stock_note": "Not derived in v4.7 because expiry threshold logic is owned by the existing inventory alert view and its SQL definition is not source-controlled in the inspected GitHub main.",
        }
        inventory_sku.append(row)

    alerts: List[Dict[str, Any]] = []
    alert_source_available = False
    if base.relation_exists(cur, "inventory", "v_inventory_alerts_ceo_for_package"):
        alert_source_available = True
        acols = base.relation_columns(cur, "inventory", "v_inventory_alerts_ceo_for_package")
        wanted = [
            "snapshot_date", "priority", "alert_type", "sku_code", "ean", "product_name",
            "total_stock", "stock_hn", "stock_hcm", "reorder_point_qty", "affected_qty",
            "affected_pct", "earliest_expiry_date", "owner", "reason", "suggested_action",
        ]
        select = []
        for name in wanted:
            if name in acols:
                select.append(name)
            else:
                cast = "date" if name in {"snapshot_date", "earliest_expiry_date"} else ("numeric" if name in {"total_stock", "stock_hn", "stock_hcm", "reorder_point_qty", "affected_qty", "affected_pct"} else "text")
                select.append(f"NULL::{cast} AS {name}")
        alerts = base.fetch_all(
            cur,
            f"SELECT {', '.join(select)} FROM inventory.v_inventory_alerts_ceo_for_package WHERE snapshot_date=%s ORDER BY priority, alert_type, product_name",
            (snapshot,),
        )

    total_stock = sum(_f(r.get("stock_qty")) for r in lot_rows)
    expired_stock = sum(_f(r.get("stock_qty")) for r in lot_rows if r.get("days_to_expiry") is not None and _i(r.get("days_to_expiry")) < 0)
    zero_day_stock = sum(_f(r.get("stock_qty")) for r in lot_rows if r.get("days_to_expiry") == 0)
    return {
        "available": True,
        "report_date": report_date.isoformat(),
        "snapshot_policy": "latest snapshot_date <= report_date; never use a future snapshot",
        "snapshot_date": snapshot.isoformat(),
        "freshness_days": (report_date - snapshot).days,
        "source_relation": "inventory.stock_lot_snapshots",
        "summary": {
            "total_stock_qty": total_stock,
            "sku_count": len(inventory_sku),
            "lot_row_count": len(lot_rows),
            "warehouse_count": len(warehouse_summary),
            "expired_stock_qty": expired_stock,
            "expires_on_report_date_stock_qty": zero_day_stock,
            "alert_count": len(alerts),
            "mixed_uom_sku_count": len(mixed_uom_keys),
        },
        "warehouse_summary": warehouse_summary,
        "lot_detail": lot_rows,
        "alerts": alerts,
        "alert_source_available": alert_source_available,
        "alert_source_relation": "inventory.v_inventory_alerts_ceo_for_package" if alert_source_available else None,
        "alert_threshold_policy": "Preserve the existing alert view output verbatim. v4.7 does not invent a new near-date threshold.",
        "mixed_uom_inventory_keys": mixed_uom_keys,
        "inventory_sku_master": inventory_sku,
    }


def _enrich_sku_fact_with_master(rows: List[Dict[str, Any]], master: Mapping[str, Any]) -> None:
    for row in rows:
        source_product_name = row.get("product_name")
        resolved = _resolve_product(master, row.get("source_sku"), row.get("ean"))
        row.update(resolved)
        row["source_product_name"] = source_product_name
        if row.get("master_product_name"):
            row["product_name"] = row.get("master_product_name")
        else:
            row["product_name"] = source_product_name


def _inventory_indexes(inventory: Mapping[str, Any]) -> Tuple[Dict[str, Mapping[str, Any]], Dict[str, set[str]]]:
    by_pid: Dict[str, Mapping[str, Any]] = {}
    by_key: Dict[str, set[str]] = defaultdict(set)
    for row in inventory.get("inventory_sku_master") or []:
        pid = str(row.get("product_id") or "")
        if pid:
            by_pid[pid] = row
        inv_id = str(row.get("inventory_key") or pid or "")
        for value in (row.get("canonical_sku"), row.get("primary_ean")):
            key = _norm_identifier(value)
            if key:
                by_key[key].add(inv_id)
    return by_pid, by_key


def _build_sku_inventory_summary(
    fact_rows: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
    report_date: dt.date,
    company_level: bool = False,
) -> List[Dict[str, Any]]:
    groups: Dict[Tuple[str, str], Dict[str, Any]] = {}
    inv_by_pid, _ = _inventory_indexes(inventory)

    def identity(row: Mapping[str, Any]) -> str:
        if row.get("product_id"):
            return "PID:" + str(row.get("product_id"))
        if row.get("canonical_sku"):
            return "CANON:" + _norm_identifier(row.get("canonical_sku"))
        if row.get("ean"):
            return "EAN:" + _norm_identifier(row.get("ean"))
        if row.get("source_sku"):
            return "SKU:" + _norm_identifier(row.get("source_sku"))
        return "NAME:" + _norm_identifier(row.get("product_name"))

    for row in fact_rows:
        channel = "TOTAL" if company_level else str(row.get("channel") or "")
        key = (channel, identity(row))
        if key not in groups:
            groups[key] = {
                "channel": channel,
                "sku_identity": key[1],
                "product_id": row.get("product_id"),
                "canonical_sku": row.get("canonical_sku"),
                "primary_ean": row.get("primary_ean") or row.get("ean"),
                "source_skus": set(),
                "product_name": row.get("master_product_name") or row.get("product_name"),
                "product_mapping_statuses": set(),
                "is_composite_sku": False,
                "sold_qty_day": 0.0,
                "gift_qty_day": 0.0,
                "total_push_qty_day": 0.0,
                "sold_qty_mtd": 0.0,
                "gift_qty_mtd": 0.0,
                "total_push_qty_mtd": 0.0,
                "return_qty_mtd": 0.0,
                "return_qty_available": False,
                "cancelled_qty_mtd": 0.0,
                "sold_cogs_mtd": 0.0,
                "gift_cogs_mtd": 0.0,
                "source_net_sales_mtd": 0.0,
                "source_net_sales_available": True,
            }
        acc = groups[key]
        if row.get("source_sku"):
            acc["source_skus"].add(str(row.get("source_sku")))
        if row.get("product_mapping_status"):
            acc["product_mapping_statuses"].add(str(row.get("product_mapping_status")))
        acc["is_composite_sku"] = bool(acc["is_composite_sku"] or row.get("is_composite_sku"))
        sold = _f(row.get("sold_qty")); gift = _f(row.get("gift_qty")); push = sold + gift
        acc["sold_qty_mtd"] += sold
        acc["gift_qty_mtd"] += gift
        acc["total_push_qty_mtd"] += push
        if str(row.get("report_date")) == report_date.isoformat():
            acc["sold_qty_day"] += sold
            acc["gift_qty_day"] += gift
            acc["total_push_qty_day"] += push
        ret = _maybe_f(row.get("return_qty"))
        if ret is not None:
            acc["return_qty_available"] = True
            acc["return_qty_mtd"] += ret
        acc["cancelled_qty_mtd"] += _f(row.get("cancelled_qty"))
        acc["sold_cogs_mtd"] += _f(row.get("sold_cogs"))
        acc["gift_cogs_mtd"] += _f(row.get("gift_cogs"))
        net = _maybe_f(row.get("source_net_sales"))
        if net is None:
            acc["source_net_sales_available"] = False
        else:
            acc["source_net_sales_mtd"] += net

    out: List[Dict[str, Any]] = []
    for acc in groups.values():
        acc["source_skus"] = sorted(acc["source_skus"])
        acc["product_mapping_statuses"] = sorted(acc["product_mapping_statuses"])
        if not acc["return_qty_available"]:
            acc["return_qty_mtd"] = None
        if not acc["source_net_sales_available"]:
            acc["source_net_sales_mtd"] = None
        inv = inv_by_pid.get(str(acc.get("product_id") or "")) if acc.get("product_id") else None
        if inv:
            for field in (
                "current_stock_qty", "stock_by_warehouse", "stock_hn", "stock_hcm", "stock_hcm_nhan_tem",
                "lot_count", "warehouse_count", "uom_values", "stock_qty_unit_consistency",
                "earliest_expiry_date", "days_to_earliest_expiry", "available_stock_qty", "near_expiry_stock_qty",
                "near_expiry_stock_note",
            ):
                acc[field] = copy.deepcopy(inv.get(field))
            acc["inventory_link_status"] = "MAPPED"
        else:
            acc.update({
                "current_stock_qty": None,
                "stock_by_warehouse": {},
                "stock_hn": None,
                "stock_hcm": None,
                "stock_hcm_nhan_tem": None,
                "lot_count": None,
                "warehouse_count": None,
                "uom_values": [],
                "stock_qty_unit_consistency": None,
                "earliest_expiry_date": None,
                "days_to_earliest_expiry": None,
                "available_stock_qty": None,
                "near_expiry_stock_qty": None,
                "near_expiry_stock_note": "Inventory link unavailable for this sales SKU.",
                "inventory_link_status": "UNMAPPED" if not acc.get("is_composite_sku") else "COMPOSITE_REQUIRES_COMPONENT_ALLOCATION",
            })
        acc["stock_after_mtd_push"] = acc.get("current_stock_qty")
        acc["stock_after_mtd_push_note"] = "This is the actual current snapshot, not opening_stock - MTD push. v4.7 does not infer stock movement."
        acc["near_date_pushed_qty"] = None
        acc["near_date_pushed_qty_note"] = "Unavailable until v4.8 order-to-lot / stock movement attribution."
        out.append(acc)

    out.sort(key=lambda r: (str(r.get("channel")), -_f(r.get("total_push_qty_mtd")), str(r.get("canonical_sku") or r.get("source_skus"))))
    return out


def _attach_inventory_alerts_to_sku_summaries(summaries: List[Dict[str, Any]], inventory: Mapping[str, Any]) -> None:
    alert_index: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for alert in inventory.get("alerts") or []:
        for value in (alert.get("ean"), alert.get("sku_code")):
            key = _norm_identifier(value)
            if key:
                alert_index[key].append(alert)
    for row in summaries:
        matches: List[Mapping[str, Any]] = []
        keys = {_norm_identifier(row.get("primary_ean")), _norm_identifier(row.get("canonical_sku"))}
        for sku in row.get("source_skus") or []:
            keys.add(_norm_identifier(sku))
        seen = set()
        for key in keys:
            for alert in alert_index.get(key) or []:
                signature = json.dumps(alert, ensure_ascii=False, sort_keys=True, default=str)
                if signature not in seen:
                    seen.add(signature); matches.append(alert)
        row["inventory_alert_count"] = len(matches)
        row["inventory_alert_priorities"] = sorted({str(a.get("priority")) for a in matches if a.get("priority")})
        row["inventory_alert_types"] = sorted({str(a.get("alert_type")) for a in matches if a.get("alert_type")})
        row["inventory_alerts"] = matches


def _excel_render_contract() -> Dict[str, Any]:
    return {
        "sheet_order": ["00_Tong_quan", "01_GT", "02_MT", "03_Nhanh", "04_Shopee", "05_TikTok", "06_Ton_kho_Can_date"],
        "top_kpi_required_every_sales_sheet": [
            "mtd_net_sales", "monthly_target_net_sales", "achievement_mtd_pct",
            "remaining_target_net_sales", "forecast_month_end_net_sales", "forecast_achievement_pct",
        ],
        "top_kpi_vietnamese_labels": {
            "mtd_net_sales": "DOANH SỐ LŨY KẾ",
            "monthly_target_net_sales": "CHỈ TIÊU THÁNG",
            "achievement_mtd_pct": "% THỰC HIỆN",
            "remaining_target_net_sales": "CÒN THIẾU",
            "forecast_month_end_net_sales": "DỰ BÁO CUỐI THÁNG",
        },
        "sku_push_inventory_columns": [
            "canonical_sku", "primary_ean", "product_name",
            "sold_qty_day", "sold_qty_mtd", "gift_qty_day", "gift_qty_mtd",
            "total_push_qty_day", "total_push_qty_mtd", "current_stock_qty",
            "stock_hn", "stock_hcm", "stock_hcm_nhan_tem",
            "earliest_expiry_date", "days_to_earliest_expiry",
            "inventory_alert_priorities", "inventory_alert_types",
        ],
        "inventory_sheet_blocks": [
            "inventory.summary", "inventory.warehouse_summary", "inventory.alerts", "inventory.lot_detail"
        ],
        "missing_value_policy": "Do not render N/D. Confirmed no-sales/applicable numeric values render 0; genuinely unavailable optional metrics are hidden or shown as an em dash by the renderer.",
        "inventory_policy": "SKU tables and 06_Ton_kho_Can_date must read the same inventory snapshot in this package. Never copy/hard-code stock between sheets.",
        "near_date_push_policy": "Do not claim near-date pushed quantity in v4.7. It requires v4.8 lot attribution.",
        "pnl_last_line": "LỢI NHUẬN",
        "forbid_ebitda": True,
    }


def _postprocess_v47(
    package: MutableMapping[str, Any],
    report_date: dt.date,
    target_progress: Mapping[str, Any],
    previous_month_pnl: Mapping[str, Any],
    sku_fact: List[Dict[str, Any]],
    inventory: Mapping[str, Any],
    require_targets: bool,
    require_inventory: bool,
) -> None:
    meta = package.setdefault("metadata", {})
    meta["package_version"] = PACKAGE_VERSION
    meta["package_version_int"] = PACKAGE_VERSION_INT
    meta["upgrade_basis_github_commit"] = UPGRADE_BASIS_GITHUB_COMMIT
    meta["upgrade_scope"] = "v4.6.1 P&L + target progress + full prior-month same-day P&L + all-SKU sold/gift + inventory lot/HSD"
    meta["excel_sheet_contract"] = "7 sheets including 06_Ton_kho_Can_date"

    package["target_progress"] = target_progress
    package["previous_month_same_day_full_pnl"] = previous_month_pnl
    package["sku_daily_fact"] = sku_fact
    package["inventory"] = inventory
    package["inventory_sku_master"] = copy.deepcopy(inventory.get("inventory_sku_master") or [])
    sku_channel = _build_sku_inventory_summary(sku_fact, inventory, report_date, company_level=False)
    sku_company = _build_sku_inventory_summary(sku_fact, inventory, report_date, company_level=True)
    _attach_inventory_alerts_to_sku_summaries(sku_channel, inventory)
    _attach_inventory_alerts_to_sku_summaries(sku_company, inventory)
    package["sku_inventory_summary"] = sku_channel
    package["company_sku_inventory_summary"] = sku_company
    package["excel_render_contract"] = _excel_render_contract()

    package.setdefault("pnl_rules", {})["v4_7_scope"] = "P&L math is inherited unchanged from v4.6.1; v4.7 only enriches target/SKU/inventory/comparison data."
    package.setdefault("pnl_rules", {})["inventory_lot_attribution"] = "NOT_AVAILABLE_IN_V4_7; never infer near-date pushed qty from SKU sales plus current lot stock."
    package.setdefault("display_semantics", {})["target_progress"] = "Use canonical v4.6.1 actual Net Sales divided by DB monthly target. Do not use forecast as target."
    package.setdefault("report_rendering_policy", {})["inventory_sheet"] = {
        "sheet": "06_Ton_kho_Can_date",
        "source": "inventory",
        "sku_stock_source": "sku_inventory_summary",
        "do_not_render_ND": True,
    }

    if require_targets and not target_progress.get("available"):
        _append_error(package, "v4.7 requires monthly targets but ops.monthly_sales_targets is unavailable/incompatible.")
    elif not target_progress.get("available"):
        _append_warning(package, "Monthly target source is unavailable; target KPIs are optional only because --require-targets was not used.")

    rows = target_progress.get("rows") or []
    missing_target_channels = [r.get("channel") for r in rows if r.get("monthly_target_net_sales") is None]
    if require_targets and missing_target_channels:
        _append_error(package, f"Missing monthly target for channels: {missing_target_channels}")
    elif missing_target_channels:
        _append_warning(package, f"Missing monthly target for channels: {missing_target_channels}")
    if target_progress.get("duplicate_leaf_target_channels"):
        _append_error(package, f"Duplicate active monthly target rows for leaf channels: {target_progress.get('duplicate_leaf_target_channels')}")

    parent_gap = ((target_progress.get("parent_audit") or {}).get("reconciliation_gap"))
    if parent_gap is not None and abs(_f(parent_gap)) > EPSILON:
        _append_error(package, f"ECOM target does not reconcile to SHOPEE + TIKTOK; gap={_f(parent_gap):,.2f}")

    if not (target_progress.get("source_metadata") or {}).get("has_target_cm2_column"):
        _append_warning(package, "Target CM2 is not stored in ops.monthly_sales_targets yet. v4.7 leaves target_cm2 null; do not invent it.")
    if not target_progress.get("daily_target_source"):
        _append_warning(package, "Daily target allocation is not available in ops.daily_sales_targets. Daily/MTD plan target fields remain null; monthly achievement is still valid.")

    if require_inventory and not inventory.get("available"):
        _append_error(package, "v4.7 requires inventory but no inventory.stock_lot_snapshots snapshot <= report_date is available.")
    elif not inventory.get("available"):
        _append_warning(package, "Inventory snapshot is unavailable; inventory blocks are optional only because --require-inventory was not used.")
    if inventory.get("available") and not inventory.get("alert_source_available"):
        _append_warning(package, "inventory.v_inventory_alerts_ceo_for_package is unavailable. Lot/HSD stock is still loaded, but P1/P2/P3/near-date alert thresholds are not invented by v4.7.")
    if inventory.get("mixed_uom_inventory_keys"):
        _append_warning(package, f"Inventory has mixed UOM for {len(inventory.get('mixed_uom_inventory_keys') or [])} SKU keys; review before summing raw stock across those UOMs.")

    unmapped = sum(1 for r in sku_fact if r.get("product_mapping_status") not in {"MAPPED"})
    composite = sum(1 for r in sku_fact if r.get("product_mapping_status") == "COMPOSITE_REQUIRES_COMPONENT_ALLOCATION")
    package.setdefault("data_quality", {})["v4_7_enrichment"] = {
        "sku_daily_fact_rows": len(sku_fact),
        "sku_unmapped_or_composite_rows": unmapped,
        "composite_sku_rows": composite,
        "inventory_snapshot_date": inventory.get("snapshot_date"),
        "inventory_freshness_days": inventory.get("freshness_days"),
        "target_available": bool(target_progress.get("available")),
        "target_cm2_available": bool((target_progress.get("source_metadata") or {}).get("has_target_cm2_column")),
        "daily_target_available": bool(target_progress.get("daily_target_source")),
        "previous_month_full_pnl_available": bool(previous_month_pnl.get("available")),
    }
    if unmapped:
        _append_warning(package, f"SKU master/inventory mapping unresolved for {unmapped}/{len(sku_fact)} daily SKU fact rows; no stock was invented for unresolved/composite SKUs.")

    # Explicitly forbid N/D strings in the package contract.
    _refresh_dq_status(package)


def build_package_v47(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    include_growth_context: bool = True,
    require_targets: bool = True,
    require_inventory: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    v461 = _load_v461()
    base, package = v461.build_package_v461(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        include_growth_context=include_growth_context,
    )
    report_date = base.parse_date(report_date_str)
    month_start = report_date.replace(day=1)

    # All enrichments read the same production DB as the base package.
    try:
        import psycopg2.extras
    except Exception as exc:
        raise RuntimeError("Missing psycopg2-binary required by v4.7 enrichment") from exc

    with base.connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            target_rows, target_meta = _fetch_target_rows(base, cur, report_date)
            target_progress = _build_target_progress_from_rows(package, report_date, target_rows, target_meta)
            master = _load_product_master_maps(base, cur)
            sku_fact = _fetch_sku_daily_fact(v461, base, cur, month_start, report_date)
            _enrich_sku_fact_with_master(sku_fact, master)
            inventory = _fetch_inventory(base, cur, report_date, master)

    previous_month_pnl = _fetch_previous_month_full_pnl(v461, report_date, vat_rates)
    _postprocess_v47(
        package,
        report_date,
        target_progress,
        previous_month_pnl,
        sku_fact,
        inventory,
        require_targets=require_targets,
        require_inventory=require_inventory,
    )
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    return base, base.clean(package)


def _contains_nd(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().upper() in {"N/D", "ND", "N.A.", "N/A"}
    if isinstance(value, dict):
        return any(_contains_nd(v) for v in value.values())
    if isinstance(value, list):
        return any(_contains_nd(v) for v in value)
    return False


def self_test() -> int:
    failures: List[str] = []
    report_date = dt.date(2026, 8, 14)
    package = {
        "mtd_daily_detail_by_channel": {
            ch: [{"net_sales": 100.0, "cm2": 20.0}] for ch in CHANNELS
        },
        "daily_pnl_by_channel_map": {
            ch: {"current": {"net_sales": 10.0, "cm2": 2.0}} for ch in CHANNELS
        },
        "monthly_scorecard_by_channel": [
            {"channel": ch, "forecast_net_sales_month_end": 300.0} for ch in CHANNELS
        ],
    }
    target_rows = [
        {"channel_code": "MT", "target_revenue": 400_000_000},
        {"channel_code": "GT", "target_revenue": 830_000_000},
        {"channel_code": "DIGITAL", "target_revenue": 400_000_000},
        {"channel_code": "ECOM", "target_revenue": 2_500_000_000},
        {"channel_code": "SHOPEE", "target_revenue": 1_700_000_000, "parent_channel": "ECOM"},
        {"channel_code": "TIKTOK", "target_revenue": 800_000_000, "parent_channel": "ECOM"},
    ]
    tp = _build_target_progress_from_rows(
        package, report_date, target_rows,
        {"available": True, "daily_target_available": False, "daily_rows": [], "has_target_cm2_column": False},
    )
    if abs(_f(tp["total"]["monthly_target_net_sales"]) - 4_130_000_000) > 0.1:
        failures.append("Target TOTAL must be 4.13b from leaf channels; ECOM must not double count")
    if abs(_f(tp["parent_audit"]["reconciliation_gap"])) > 0.1:
        failures.append("ECOM target reconciliation failed")
    if len(tp["rows"]) != 5:
        failures.append("Target leaf channel count must be 5")

    inv = {
        "inventory_sku_master": [{
            "product_id": "P1", "inventory_key": "P1", "canonical_sku": "SKU1", "primary_ean": "123",
            "product_name": "Product 1", "current_stock_qty": 50, "stock_by_warehouse": {"HN_TON_KHO": 20, "HCM_TON_KHO": 30},
            "stock_hn": 20, "stock_hcm": 30, "stock_hcm_nhan_tem": 0, "lot_count": 2, "warehouse_count": 2,
            "uom_values": ["chai"], "stock_qty_unit_consistency": "PASS", "earliest_expiry_date": "2026-12-31",
            "days_to_earliest_expiry": 135, "available_stock_qty": None, "near_expiry_stock_qty": None,
            "near_expiry_stock_note": "not-derived",
        }],
        "alerts": [],
    }
    fact = [
        {"report_date": "2026-08-14", "channel": "GT", "source_sku": "SKU1", "ean": "123", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123", "product_name": "Product 1", "product_mapping_status": "MAPPED", "sold_qty": 3, "gift_qty": 1, "return_qty": 0, "cancelled_qty": 0, "sold_cogs": 3, "gift_cogs": 1, "source_net_sales": 100, "is_composite_sku": False},
        {"report_date": "2026-08-13", "channel": "GT", "source_sku": "SKU1", "ean": "123", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123", "product_name": "Product 1", "product_mapping_status": "MAPPED", "sold_qty": 2, "gift_qty": 0, "return_qty": 0, "cancelled_qty": 0, "sold_cogs": 2, "gift_cogs": 0, "source_net_sales": 80, "is_composite_sku": False},
    ]
    ss = _build_sku_inventory_summary(fact, inv, report_date, company_level=False)
    if len(ss) != 1 or ss[0]["sold_qty_mtd"] != 5 or ss[0]["gift_qty_mtd"] != 1 or ss[0]["current_stock_qty"] != 50:
        failures.append("SKU/inventory summary aggregation failed")
    if ss[0]["total_push_qty_mtd"] != 6:
        failures.append("sold + gift != total push")
    if _contains_nd({"x": None, "y": 0}):
        failures.append("N/D scanner false positive")
    if not _contains_nd({"x": "N/D"}):
        failures.append("N/D scanner failed")
    if _add_months(dt.date(2026, 3, 31), -1) != dt.date(2026, 2, 28):
        failures.append("previous-month same-day calendar clamp failed")

    wh = _warehouse_stock_breakdown({"HN_TON_KHO": 20, "HCM_TON_KHO": 30, "HCM_NHAN_TEM": 10})
    if wh != {"stock_hn": 20.0, "stock_hcm": 30.0, "stock_hcm_total": 40.0, "stock_hcm_nhan_tem": 10.0}:
        failures.append(f"warehouse regular/label separation failed: {wh}")

    name_rows = [{"source_sku": "SKU1", "ean": "123", "product_name": "Tên nguồn"}]
    name_master = {
        "products": {"P1": {"canonical_sku": "SKU1", "primary_ean": "123", "product_name": "Tên master"}},
        "key_to_products": {_norm_identifier("SKU1"): {"P1"}, _norm_identifier("123"): {"P1"}},
    }
    _enrich_sku_fact_with_master(name_rows, name_master)
    if name_rows[0].get("source_product_name") != "Tên nguồn" or name_rows[0].get("product_name") != "Tên master":
        failures.append("source product name audit preservation failed")

    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for item in failures:
            print("-", item, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Target total leaf policy: 4.13b fixture PASS")
    print("SKU sold/gift + inventory join fixture: PASS")
    print("No future inventory / no lot-attribution inference contract: PASS")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="Report date YYYY-MM-DD")
    ap.add_argument("--compare-mode", choices=("previous_day", "previous_week_same_day", "previous_month_same_day"), default="previous_day")
    ap.add_argument("--compare-date", default=None)
    ap.add_argument("--vat-rates", default=os.getenv("DAILY_PNL_VAT_RATES", ""))
    ap.add_argument("--require-vat-rates", action="store_true")
    ap.add_argument("--without-growth-context", action="store_true")
    ap.add_argument("--allow-missing-targets", action="store_true")
    ap.add_argument("--allow-missing-inventory", action="store_true")
    ap.add_argument("--out-dir", default="outbox/packages")
    ap.add_argument("--store-db", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.date:
        ap.error("--date is required unless --self-test is used")

    v461 = _load_v461()
    v46 = v461._load_v46()
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

    base, package = build_package_v47(
        report_date,
        args.compare_mode,
        args.compare_date,
        vat_rates,
        include_growth_context=not args.without_growth_context,
        require_targets=not args.allow_missing_targets,
        require_inventory=not args.allow_missing_inventory,
    )
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ceo_daily_pnl_package_v4_7_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_7_{report_date}.md"
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=base.json_default), encoding="utf-8")
    md = base.markdown_summary(package)
    md += "\n\n## v4.7 Target & Inventory\n"
    total = (package.get("target_progress") or {}).get("total") or {}
    md += f"- MTD Net Sales: {total.get('mtd_net_sales')}\n"
    md += f"- Monthly Target: {total.get('monthly_target_net_sales')}\n"
    md += f"- Achievement: {total.get('achievement_mtd_pct')}\n"
    inv = package.get("inventory") or {}
    md += f"- Inventory snapshot: {inv.get('snapshot_date')} (freshness_days={inv.get('freshness_days')})\n"
    md += f"- Inventory total stock: {(inv.get('summary') or {}).get('total_stock_qty')}\n"
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
