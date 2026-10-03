#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.7.5 — GT special-employee reporting split.

Business contract added in v4.7.5
---------------------------------
- Employee NV001 / NGUYỄN XUÂN MẠNH remains a GT employee in mtgt.sales_lines.
- CEO reporting excludes this employee from the displayed GT total.
- The employee is rendered as a separate reporting row/block under GT.
- Company TOTAL still includes both GT-ex-NV001 and NV001, so no revenue disappears.
- NV001 uses normal GT cost rules currently used by HH reporting:
    COGS = mtgt.sales_lines.quantity * unit_cogs
    logistics/shipping = 2% of Net Sales
    backoffice = 15% of Net Sales
    platform/live/booking/packaging = 0
  but advertising cost is ALWAYS 0 for this employee.
- The legacy inherited v4.7.4 blocks are preserved for backward compatibility.
  New renderers MUST use the v4.7.5 reporting_* blocks described in
  excel_render_contract.v4_7_5_gt_special_employee.
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

PACKAGE_VERSION = "v4.7.5"
PACKAGE_VERSION_INT = 475
BASE_FILENAME = "build_ceo_daily_pnl_package_v4_7_4.py"
EPSILON = 1.0

SPECIAL_EMPLOYEE_CODE = "NV001"
SPECIAL_EMPLOYEE_NAME = "NGUYỄN XUÂN MẠNH"
SPECIAL_CHANNEL_KEY = "GT_NV001"
SPECIAL_CHANNEL_DISPLAY = "GT - Nguyễn Xuân Mạnh"
SPECIAL_PARENT_CHANNEL = "GT"
GT_SHIPPING_RATE = 0.02
GT_BACKOFFICE_RATE = 0.15

REPORTING_CHANNELS: Tuple[str, ...] = (
    "GT", SPECIAL_CHANNEL_KEY, "MT", "NHANH", "SHOPEE", "TIKTOK"
)

ADDITIVE_PNL_FIELDS: Tuple[str, ...] = (
    "gross_sales_source", "gross_sales", "gross_non_cancelled_sales", "gross_cancelled_sales",
    "seller_discount_source", "seller_voucher_source", "seller_discount", "seller_voucher",
    "other_sales_adjustment",
    "sales_deductions_total", "sales_deductions_component_sum", "sales_deductions_reconciliation_gap",
    "net_sales_source", "net_sales", "orders", "success_orders", "cancelled_orders",
    "cancelled_orders_with_gross", "sold_cogs", "gift_cogs", "cogs_total",
    "cogs_split_reconciliation_gap", "gm1", "fixed_fee", "service_fee", "payment_fee",
    "vxp_fee", "infrastructure_fee", "fulfillment_fee", "packaging_cost", "logistics_order_cost",
    "cancel_related_shipping_fee", "cm1_cost_total", "cm1", "ads_product", "ads_live",
    "teaser_campaign", "ads_other", "ads_total", "ads_cost_reported_legacy",
    "ads_reconciliation_gap", "affiliate_creator", "affiliate_partner", "affiliate_other",
    "affiliate_total", "affiliate_total_source", "affiliate_reconciliation_gap",
    "affiliate_order_count_detail", "booking_kol_koc", "livestream_inhouse", "content_cost",
    "trade_marketing_cost", "agency_campaign_direct_cost", "cm2_cost_total", "cm2",
    "backoffice_cost", "profit", "platform_fees_reported_legacy",
    "platform_components_ex_affiliate", "platform_components_plus_affiliate",
    "platform_expected_legacy", "platform_reconciliation_gap", "legacy_profit",
    "profit_vs_legacy_profit", "payout", "settlement_paid", "settlement_unpaid", "estimated_payout",
)

RECON_FIELDS: Tuple[str, ...] = (
    "gross_sales", "net_sales", "orders", "sold_cogs", "gift_cogs", "cogs_total",
    "gm1", "logistics_order_cost", "cm1", "ads_total", "cm2", "backoffice_cost", "profit",
)


def _load_v474():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.5 must be installed on top of v4.7.4")
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v474_base", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _f(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        x = float(value)
        return default if math.isnan(x) or math.isinf(x) else x
    except Exception:
        return default


def _pct(num: Any, den: Any) -> Optional[float]:
    d = _f(den)
    if abs(d) < 1e-12:
        return None
    return _f(num) / d


def _norm(value: Any) -> str:
    text = unicodedata.normalize("NFC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text.casefold()


def _date_range(start: dt.date, end: dt.date) -> Iterable[dt.date]:
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def _zero_special_row(day: dt.date) -> Dict[str, Any]:
    return _staff_pnl_from_raw({"report_date": day.isoformat()})


def _staff_pnl_from_raw(raw: Mapping[str, Any]) -> Dict[str, Any]:
    day = str(raw.get("report_date") or "")
    gross = _f(raw.get("gross_sales"))
    net = _f(raw.get("net_sales"))
    sold_cogs = _f(raw.get("sold_cogs"))
    gift_cogs = _f(raw.get("gift_cogs"))
    cogs = sold_cogs + gift_cogs
    orders = int(round(_f(raw.get("orders"))))
    logistics = net * GT_SHIPPING_RATE
    backoffice = net * GT_BACKOFFICE_RATE
    deductions = gross - net
    gm1 = net - cogs
    cm1 = gm1 - logistics
    cm2 = cm1  # Explicitly no advertising or other CM2 cost for NV001.
    profit = cm2 - backoffice
    return {
        "report_date": day,
        "channel": SPECIAL_CHANNEL_KEY,
        "display_channel": SPECIAL_CHANNEL_DISPLAY,
        "parent_channel": SPECIAL_PARENT_CHANNEL,
        "employee_code": SPECIAL_EMPLOYEE_CODE,
        "employee_name": SPECIAL_EMPLOYEE_NAME,
        "vat_rate": 0.0,
        "gross_sales_source": gross,
        "gross_sales": gross,
        "gross_non_cancelled_sales": gross,
        "gross_cancelled_sales": 0.0,
        "gross_status_reconciliation_gap": 0.0,
        "net_sales_source": net,
        "seller_discount_source": 0.0,
        "seller_voucher_source": 0.0,
        "seller_discount": 0.0,
        "seller_voucher": 0.0,
        "other_sales_adjustment": deductions,
        "sales_deductions_reported_source": deductions,
        "sales_deductions_reported": deductions,
        "sales_deductions_total": deductions,
        "sales_deductions_component_sum": deductions,
        "sales_deductions_reconciliation_gap": 0.0,
        "net_sales": net,
        "orders": orders,
        "success_orders": orders,
        "cancelled_orders": 0,
        "cancellation_rate": 0.0,
        "cancelled_orders_with_gross": 0,
        "sold_cogs": sold_cogs,
        "gift_cogs": gift_cogs,
        "cogs_total": cogs,
        "cogs_split_available": True,
        "cogs_split_reconciliation_gap": 0.0,
        "gm1": gm1,
        "gm1_margin": _pct(gm1, net),
        "fixed_fee": 0.0,
        "service_fee": 0.0,
        "payment_fee": 0.0,
        "vxp_fee": 0.0,
        "infrastructure_fee": 0.0,
        "fulfillment_fee": 0.0,
        "packaging_cost": 0.0,
        "logistics_order_cost": logistics,
        "cancel_related_shipping_fee": 0.0,
        "cm1_cost_total": logistics,
        "cm1": cm1,
        "cm1_margin": _pct(cm1, net),
        "ads_product": 0.0,
        "ads_live": 0.0,
        "teaser_campaign": 0.0,
        "ads_other": 0.0,
        "ads_total": 0.0,
        "ads_cost_reported_legacy": 0.0,
        "ads_reconciliation_gap": 0.0,
        "affiliate_creator": 0.0,
        "affiliate_partner": 0.0,
        "affiliate_other": 0.0,
        "affiliate_total": 0.0,
        "affiliate_total_source": 0.0,
        "affiliate_reconciliation_gap": 0.0,
        "affiliate_order_count_detail": 0,
        "booking_kol_koc": 0.0,
        "livestream_inhouse": 0.0,
        "content_cost": 0.0,
        "trade_marketing_cost": 0.0,
        "agency_campaign_direct_cost": 0.0,
        "cm2_cost_total": 0.0,
        "cm2": cm2,
        "cm2_margin": _pct(cm2, net),
        "backoffice_cost": backoffice,
        "profit": profit,
        "profit_margin": _pct(profit, net),
        "platform_fees_reported_legacy": 0.0,
        "platform_components_ex_affiliate": 0.0,
        "platform_components_plus_affiliate": 0.0,
        "legacy_platform_includes_affiliate": False,
        "platform_expected_legacy": 0.0,
        "platform_reconciliation_gap": 0.0,
        "legacy_profit": profit,
        "profit_vs_legacy_profit": 0.0,
        "payout": net,
        "settlement_paid": 0.0,
        "settlement_unpaid": 0.0,
        "estimated_payout": net,
        "applicable_cost_codes": ["sold_cogs", "gift_cogs", "logistics_order_cost", "backoffice_cost"],
        "source_map": {
            "source": "mtgt.sales_lines",
            "employee_filter": f"employee_code={SPECIAL_EMPLOYEE_CODE}",
            "gross_sales": "sum(gross_revenue) excluding CVC",
            "net_sales": "sum(net_revenue) excluding CVC",
            "cogs": "sum(quantity * unit_cogs) excluding CVC",
            "logistics_order_cost": f"{GT_SHIPPING_RATE:.0%} * net_sales (normal GT policy)",
            "backoffice_cost": f"{GT_BACKOFFICE_RATE:.0%} * net_sales (normal GT policy)",
            "ads": "FORCED_ZERO_BY_OWNER_POLICY",
        },
    }


def _recalc_pnl(row: MutableMapping[str, Any]) -> None:
    gross = _f(row.get("gross_sales"))
    net = _f(row.get("net_sales"))
    sold_cogs = _f(row.get("sold_cogs"))
    gift_cogs = _f(row.get("gift_cogs"))
    cogs = sold_cogs + gift_cogs
    row["cogs_total"] = cogs
    row["sales_deductions_total"] = gross - net
    row["sales_deductions_component_sum"] = (
        _f(row.get("gross_cancelled_sales")) + _f(row.get("seller_discount"))
        + _f(row.get("seller_voucher")) + _f(row.get("other_sales_adjustment"))
    )
    row["sales_deductions_reconciliation_gap"] = row["sales_deductions_total"] - row["sales_deductions_component_sum"]
    row["cogs_split_reconciliation_gap"] = cogs - sold_cogs - gift_cogs
    gm1 = net - cogs
    row["gm1"] = gm1
    row["gm1_margin"] = _pct(gm1, net)
    cm1_cost = sum(_f(row.get(k)) for k in (
        "fixed_fee", "service_fee", "payment_fee", "vxp_fee", "infrastructure_fee",
        "fulfillment_fee", "packaging_cost", "logistics_order_cost", "cancel_related_shipping_fee"
    ))
    row["cm1_cost_total"] = cm1_cost
    row["cm1"] = gm1 - cm1_cost
    row["cm1_margin"] = _pct(row["cm1"], net)
    cm2_cost = sum(_f(row.get(k)) for k in (
        "ads_product", "ads_live", "teaser_campaign", "ads_other", "affiliate_total",
        "booking_kol_koc", "livestream_inhouse", "content_cost", "trade_marketing_cost",
        "agency_campaign_direct_cost"
    ))
    row["ads_total"] = sum(_f(row.get(k)) for k in ("ads_product", "ads_live", "teaser_campaign", "ads_other"))
    row["cm2_cost_total"] = cm2_cost
    row["cm2"] = row["cm1"] - cm2_cost
    row["cm2_margin"] = _pct(row["cm2"], net)
    row["profit"] = row["cm2"] - _f(row.get("backoffice_cost"))
    row["profit_margin"] = _pct(row["profit"], net)
    row["legacy_profit"] = row["profit"]
    row["profit_vs_legacy_profit"] = 0.0
    orders = max(int(round(_f(row.get("orders")))), 0)
    cancelled = max(int(round(_f(row.get("cancelled_orders")))), 0)
    row["orders"] = orders
    row["cancelled_orders"] = cancelled
    row["success_orders"] = max(orders - cancelled, 0)
    row["cancellation_rate"] = _pct(cancelled, orders) or 0.0


def _subtract_staff_from_gt(gt: Mapping[str, Any], staff: Mapping[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(dict(gt))
    out["channel"] = "GT"
    for key in ADDITIVE_PNL_FIELDS:
        if key in out or abs(_f(staff.get(key))) > 1e-12:
            out[key] = _f(out.get(key)) - _f(staff.get(key))
    # Staff is not assigned advertising, so all GT advertising remains on GT.
    for key in ("ads_product", "ads_live", "teaser_campaign", "ads_other", "ads_total", "ads_cost_reported_legacy"):
        out[key] = _f(gt.get(key))
    _recalc_pnl(out)
    source_map = copy.deepcopy(out.get("source_map") or {})
    source_map["v4_7_5_gt_special_employee_exclusion"] = (
        f"Displayed GT excludes {SPECIAL_EMPLOYEE_CODE} / {SPECIAL_EMPLOYEE_NAME}; advertising remains fully on GT."
    )
    out["source_map"] = source_map
    out["reporting_scope"] = "GT_EXCLUDING_SPECIAL_EMPLOYEE"
    return out


def _fetch_special_daily(base, cur, start: dt.date, end: dt.date) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if not base.relation_exists(cur, "mtgt", "sales_lines"):
        return [], {"available": False, "reason": "mtgt.sales_lines missing"}
    cols = base.relation_columns(cur, "mtgt", "sales_lines")
    required = {"sale_date", "channel_group", "employee_code", "net_revenue", "gross_revenue", "quantity", "unit_cogs"}
    missing = sorted(required - cols)
    if missing:
        return [], {"available": False, "reason": f"mtgt.sales_lines missing columns {missing}"}
    qsale = "COALESCE(s.quantity_sale,0)" if "quantity_sale" in cols else "GREATEST(COALESCE(s.quantity,0),0)"
    qgift = "COALESCE(s.quantity_gift,0)" if "quantity_gift" in cols else "0::numeric"
    qret = "COALESCE(s.quantity_return,0)" if "quantity_return" in cols else "0::numeric"
    doc = "s.document_no" if "document_no" in cols else "s.id::text"
    emp_name = "COALESCE(NULLIF(btrim(s.employee_name),''),'')" if "employee_name" in cols else "''"
    rows = base.fetch_all(cur, f"""
        SELECT
            s.sale_date::date AS report_date,
            SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.gross_revenue,0) ELSE 0 END)::numeric AS gross_sales,
            SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN COALESCE(s.net_revenue,0) ELSE 0 END)::numeric AS net_sales,
            SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN ({qsale} - {qret}) * COALESCE(s.unit_cogs,0) ELSE 0 END)::numeric AS sold_cogs,
            SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN {qgift} * COALESCE(s.unit_cogs,0) ELSE 0 END)::numeric AS gift_cogs,
            SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN GREATEST({qsale},0) ELSE 0 END)::numeric AS sold_qty,
            SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN GREATEST({qgift},0) ELSE 0 END)::numeric AS gift_qty,
            SUM(CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN GREATEST({qret},0) ELSE 0 END)::numeric AS return_qty,
            COUNT(DISTINCT CASE WHEN COALESCE(s.line_type,'') <> 'CVC' THEN {doc} END)::numeric AS orders,
            array_agg(DISTINCT {emp_name}) AS employee_names
        FROM mtgt.sales_lines s
        WHERE s.sale_date BETWEEN %s AND %s
          AND upper(btrim(COALESCE(s.channel_group,''))) = 'GT'
          AND btrim(COALESCE(s.employee_code,'')) = %s
        GROUP BY s.sale_date::date
        ORDER BY s.sale_date::date
    """, (start, end, SPECIAL_EMPLOYEE_CODE))
    names = sorted({str(n).strip() for r in rows for n in (r.get("employee_names") or []) if str(n).strip()})
    out = [_staff_pnl_from_raw(r) for r in rows]
    return out, {
        "available": True,
        "employee_code": SPECIAL_EMPLOYEE_CODE,
        "expected_employee_name": SPECIAL_EMPLOYEE_NAME,
        "observed_employee_names": names,
        "name_match": all(_norm(n) == _norm(SPECIAL_EMPLOYEE_NAME) for n in names) if names else True,
        "row_days": len(out),
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
    }


def _fetch_special_sku_daily(base, cur, start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
    if not base.relation_exists(cur, "mtgt", "sales_lines"):
        return []
    cols = base.relation_columns(cur, "mtgt", "sales_lines")
    required = {"sale_date", "channel_group", "employee_code", "product_code"}
    if not required.issubset(cols):
        return []
    qsale = "COALESCE(s.quantity_sale,0)" if "quantity_sale" in cols else "CASE WHEN COALESCE(s.quantity,0)>0 THEN COALESCE(s.quantity,0) ELSE 0 END"
    qgift = "COALESCE(s.quantity_gift,0)" if "quantity_gift" in cols else "0::numeric"
    qret = "COALESCE(s.quantity_return,0)" if "quantity_return" in cols else "0::numeric"
    unit_cogs = "COALESCE(s.unit_cogs,0)" if "unit_cogs" in cols else "0::numeric"
    barcode = "s.barcode" if "barcode" in cols else "NULL::text"
    pname = "s.product_name" if "product_name" in cols else "NULL::text"
    net = "SUM(COALESCE(s.net_revenue,0))::numeric" if "net_revenue" in cols else "NULL::numeric"
    order_count = "COUNT(DISTINCT s.document_no)::numeric" if "document_no" in cols else "NULL::numeric"
    line_filter = "AND COALESCE(s.line_type,'') <> 'CVC'" if "line_type" in cols else ""
    rows = base.fetch_all(cur, f"""
        SELECT
            s.sale_date::date AS report_date,
            %s::text AS channel,
            %s::text AS subchannel,
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
          AND upper(btrim(COALESCE(s.channel_group,''))) = 'GT'
          AND btrim(COALESCE(s.employee_code,'')) = %s
          {line_filter}
        GROUP BY 1,4,5
        ORDER BY 1,4
    """, (SPECIAL_CHANNEL_KEY, SPECIAL_CHANNEL_DISPLAY, start, end, SPECIAL_EMPLOYEE_CODE))
    for r in rows:
        sold = max(_f(r.get("sold_qty")), 0.0)
        gift = max(_f(r.get("gift_qty")), 0.0)
        r.update({
            "channel": SPECIAL_CHANNEL_KEY,
            "subchannel": SPECIAL_CHANNEL_DISPLAY,
            "sold_qty": sold,
            "gift_qty": gift,
            "total_push_qty": sold + gift,
            "cancelled_qty": 0.0,
            "sold_cogs": _f(r.get("sold_cogs")),
            "gift_cogs": _f(r.get("gift_cogs")),
            "total_push_cogs": _f(r.get("sold_cogs")) + _f(r.get("gift_cogs")),
            "is_composite_sku": "+" in str(r.get("source_sku") or ""),
            "component_skus": [x.strip() for x in str(r.get("source_sku") or "").split("+") if x.strip()] if "+" in str(r.get("source_sku") or "") else [],
            "gift_classification_rule": "mtgt.sales_lines.quantity_gift",
            "sold_classification_rule": f"mtgt.sales_lines.quantity_sale filtered employee_code={SPECIAL_EMPLOYEE_CODE}",
            "net_sales_allocation": "exact_line_net_revenue",
            "source_relation": "mtgt.sales_lines",
        })
    return rows


def _daily_map(rows: Sequence[Mapping[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {str(r.get("report_date")): dict(r) for r in rows if r.get("report_date")}


def _split_daily_detail(
    legacy: Mapping[str, Sequence[Mapping[str, Any]]], staff_rows: Sequence[Mapping[str, Any]],
    start: dt.date, end: dt.date
) -> Dict[str, List[Dict[str, Any]]]:
    out = copy.deepcopy(dict(legacy))
    smap = _daily_map(staff_rows)
    legacy_gt = _daily_map((legacy.get("GT") or []) if isinstance(legacy, Mapping) else [])
    gt_rows: List[Dict[str, Any]] = []
    special_rows: List[Dict[str, Any]] = []
    for day in _date_range(start, end):
        key = day.isoformat()
        staff = smap.get(key) or _zero_special_row(day)
        special_rows.append(copy.deepcopy(staff))
        gt = legacy_gt.get(key)
        if gt is None:
            gt = {"report_date": key, "channel": "GT"}
        gt_rows.append(_subtract_staff_from_gt(gt, staff))
    out["GT"] = gt_rows
    out[SPECIAL_CHANNEL_KEY] = special_rows
    return out


def _sum_summary(base, rows: Sequence[Mapping[str, Any]], end: dt.date, channel: str) -> Dict[str, Any]:
    summary = dict(base.aggregate(list(rows), end))
    summary["channel"] = channel
    if channel == SPECIAL_CHANNEL_KEY:
        summary["display_channel"] = SPECIAL_CHANNEL_DISPLAY
        summary["parent_channel"] = SPECIAL_PARENT_CHANNEL
        summary["employee_code"] = SPECIAL_EMPLOYEE_CODE
        summary["employee_name"] = SPECIAL_EMPLOYEE_NAME
        summary["ads_total"] = 0.0
    return base.clean(summary)


def _build_reporting_mtd_comparison(base, current_daily, previous_daily, current_end, previous_end) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    by_channel: Dict[str, Any] = {}
    cur_summaries: Dict[str, Dict[str, Any]] = {}
    prev_summaries: Dict[str, Dict[str, Any]] = {}
    for ch in REPORTING_CHANNELS:
        cur = _sum_summary(base, current_daily.get(ch) or [], current_end, ch)
        prev = _sum_summary(base, previous_daily.get(ch) or [], previous_end, ch)
        cur_summaries[ch] = cur
        prev_summaries[ch] = prev
        by_channel[ch] = {
            "channel": ch,
            "display_channel": SPECIAL_CHANNEL_DISPLAY if ch == SPECIAL_CHANNEL_KEY else ch,
            "current_mtd": cur,
            "previous_mtd": prev,
            "pnl_lines": base.pnl_lines(cur, prev),
        }
    total_cur = dict(base.aggregate([cur_summaries[ch] for ch in REPORTING_CHANNELS], current_end))
    total_prev = dict(base.aggregate([prev_summaries[ch] for ch in REPORTING_CHANNELS], previous_end))
    total_cur["channel"] = "TOTAL"; total_prev["channel"] = "TOTAL"
    total = {"channel": "TOTAL", "current_mtd": total_cur, "previous_mtd": total_prev, "pnl_lines": base.pnl_lines(total_cur, total_prev)}
    return base.clean(by_channel), base.clean(total)


def _split_sku_fact(package: Mapping[str, Any], staff_rows: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    original = copy.deepcopy(package.get("sku_daily_fact") or [])
    # Reuse product identity already resolved on the original GT fact rows.
    original_index: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for row in original:
        if str(row.get("channel")) == "GT":
            original_index[(str(row.get("report_date")), str(row.get("source_sku") or ""), str(row.get("ean") or ""))] = row
    staff_index = {(str(r.get("report_date")), str(r.get("source_sku") or ""), str(r.get("ean") or "")): r for r in staff_rows}
    adjusted: List[Dict[str, Any]] = []
    for row in original:
        if str(row.get("channel")) != "GT":
            adjusted.append(row); continue
        key = (str(row.get("report_date")), str(row.get("source_sku") or ""), str(row.get("ean") or ""))
        staff = staff_index.get(key)
        if not staff:
            adjusted.append(row); continue
        out = copy.deepcopy(row)
        for field in ("sold_qty", "gift_qty", "return_qty", "cancelled_qty", "sold_cogs", "gift_cogs", "source_net_sales", "source_order_count"):
            if out.get(field) is not None:
                out[field] = _f(out.get(field)) - _f(staff.get(field))
        out["sold_qty"] = max(_f(out.get("sold_qty")), 0.0)
        out["gift_qty"] = max(_f(out.get("gift_qty")), 0.0)
        out["total_push_qty"] = out["sold_qty"] + out["gift_qty"]
        out["sold_cogs"] = _f(out.get("sold_cogs")); out["gift_cogs"] = _f(out.get("gift_cogs"))
        out["total_push_cogs"] = out["sold_cogs"] + out["gift_cogs"]
        adjusted.append(out)
    for staff in staff_rows:
        key = (str(staff.get("report_date")), str(staff.get("source_sku") or ""), str(staff.get("ean") or ""))
        out = copy.deepcopy(staff)
        src = original_index.get(key) or {}
        for field in (
            "product_id", "canonical_sku", "primary_ean", "master_product_name",
            "product_mapping_status", "source_product_name", "candidate_product_ids"
        ):
            if field in src:
                out[field] = copy.deepcopy(src.get(field))
        if out.get("master_product_name"):
            out["product_name"] = out.get("master_product_name")
        adjusted.append(out)
    return adjusted


def _load_v47_from_v474(v474):
    v473 = v474._load_v473()
    v472 = v473._load_v472()
    v471 = v472._load_v471()
    return v471._load_v47()


def _build_top20_from_summary(summary_rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in REPORTING_CHANNELS}
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for r in summary_rows:
        ch = str(r.get("channel") or "")
        if ch in out and _f(r.get("sold_qty_mtd")) > 0:
            grouped[ch].append(r)
    for ch in REPORTING_CHANNELS:
        rows = sorted(grouped[ch], key=lambda x: (-_f(x.get("sold_qty_mtd")), -_f(x.get("total_push_qty_mtd")), str(x.get("canonical_sku") or x.get("source_skus") or "")))[:20]
        total_sold = sum(_f(x.get("sold_qty_mtd")) for x in grouped[ch])
        for rank, src in enumerate(rows, 1):
            row = copy.deepcopy(dict(src))
            row["rank_by_sold_qty"] = rank
            row["rank_metric"] = "sold_qty_mtd_excluding_gifts"
            row["channel_sold_qty_share_pct"] = _pct(src.get("sold_qty_mtd"), total_sold)
            out[ch].append(row)
    return out


def _build_gift_view(summary_rows: Sequence[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in REPORTING_CHANNELS}
    for r in summary_rows:
        ch = str(r.get("channel") or "")
        if ch in out and _f(r.get("gift_qty_mtd")) > 0:
            out[ch].append(copy.deepcopy(dict(r)))
    for ch in REPORTING_CHANNELS:
        out[ch].sort(key=lambda x: (-_f(x.get("gift_qty_mtd")), -_f(x.get("gift_cogs_mtd")), str(x.get("canonical_sku") or x.get("source_skus") or "")))
    return out


def _rebuild_target_progress(package: Mapping[str, Any], current_daily: Mapping[str, Sequence[Mapping[str, Any]]], report_date: dt.date, base) -> Dict[str, Any]:
    original = copy.deepcopy(package.get("target_progress") or {})
    rows = copy.deepcopy(original.get("rows") or [])
    days_in_month = calendar.monthrange(report_date.year, report_date.month)[1]
    days_remaining = max(days_in_month - report_date.day, 0)
    gt_summary = _sum_summary(base, current_daily.get("GT") or [], report_date, "GT")
    special_summary = _sum_summary(base, current_daily.get(SPECIAL_CHANNEL_KEY) or [], report_date, SPECIAL_CHANNEL_KEY)
    for row in rows:
        if str(row.get("channel")) != "GT":
            continue
        monthly_target = row.get("monthly_target_net_sales")
        mtd = _f(gt_summary.get("net_sales")); day = _f((current_daily.get("GT") or [{}])[-1].get("net_sales")) if current_daily.get("GT") else 0.0
        cm2 = _f(gt_summary.get("cm2")); day_cm2 = _f((current_daily.get("GT") or [{}])[-1].get("cm2")) if current_daily.get("GT") else 0.0
        forecast = mtd / max(report_date.day, 1) * days_in_month
        remaining = None if monthly_target is None else max(_f(monthly_target) - mtd, 0.0)
        row.update({
            "mtd_net_sales": mtd,
            "day_net_sales": day,
            "achievement_mtd_pct": _pct(mtd, monthly_target),
            "remaining_target_net_sales": remaining,
            "required_daily_net_sales_remaining": (remaining / days_remaining) if (remaining is not None and days_remaining > 0) else (0.0 if remaining == 0 else None),
            "forecast_month_end_net_sales": forecast,
            "forecast_achievement_pct": _pct(forecast, monthly_target),
            "mtd_cm2": cm2,
            "day_cm2": day_cm2,
            "forecast_month_end_cm2": cm2 / max(report_date.day,1) * days_in_month,
            "daily_target_achievement_pct": _pct(day, row.get("target_day_net_sales")),
            "mtd_daily_plan_achievement_pct": _pct(mtd, row.get("target_mtd_net_sales")),
            "v4_7_5_scope": "GT excludes NV001 / Nguyễn Xuân Mạnh",
        })
    special_mtd = _f(special_summary.get("net_sales"))
    special_day = _f((current_daily.get(SPECIAL_CHANNEL_KEY) or [{}])[-1].get("net_sales")) if current_daily.get(SPECIAL_CHANNEL_KEY) else 0.0
    special_cm2 = _f(special_summary.get("cm2"))
    rows.insert(1, {
        "channel": SPECIAL_CHANNEL_KEY,
        "display_channel": SPECIAL_CHANNEL_DISPLAY,
        "parent_channel": "GT",
        "employee_code": SPECIAL_EMPLOYEE_CODE,
        "employee_name": SPECIAL_EMPLOYEE_NAME,
        "monthly_target_net_sales": None,
        "mtd_net_sales": special_mtd,
        "day_net_sales": special_day,
        "achievement_mtd_pct": None,
        "remaining_target_net_sales": None,
        "days_remaining_after_report_date": days_remaining,
        "required_daily_net_sales_remaining": None,
        "forecast_month_end_net_sales": special_mtd / max(report_date.day,1) * days_in_month,
        "forecast_achievement_pct": None,
        "target_cm2": None,
        "mtd_cm2": special_cm2,
        "day_cm2": _f((current_daily.get(SPECIAL_CHANNEL_KEY) or [{}])[-1].get("cm2")) if current_daily.get(SPECIAL_CHANNEL_KEY) else 0.0,
        "cm2_target_achievement_pct": None,
        "target_day_net_sales": None,
        "target_mtd_net_sales": None,
        "daily_target_achievement_pct": None,
        "mtd_daily_plan_achievement_pct": None,
        "target_source_channel_codes": [],
        "target_note": "No target assigned in v4.7.5; employee is reported separately and excluded from GT target actual.",
    })
    original["rows"] = rows
    # Company actual must include both GT-ex-NV001 and NV001. Target remains unchanged.
    if isinstance(original.get("total"), dict):
        total_mtd = sum(_f(r.get("mtd_net_sales")) for r in rows)
        total_day = sum(_f(r.get("day_net_sales")) for r in rows)
        target = original["total"].get("monthly_target_net_sales")
        total_forecast = sum(_f(r.get("forecast_month_end_net_sales")) for r in rows)
        total_mtd_cm2 = sum(_f(r.get("mtd_cm2")) for r in rows)
        total_day_cm2 = sum(_f(r.get("day_cm2")) for r in rows)
        total_forecast_cm2 = sum(_f(r.get("forecast_month_end_cm2")) for r in rows)
        remaining = None if target is None else max(_f(target) - total_mtd, 0.0)
        original["total"]["mtd_net_sales"] = total_mtd
        original["total"]["day_net_sales"] = total_day
        original["total"]["achievement_mtd_pct"] = _pct(total_mtd, target)
        original["total"]["remaining_target_net_sales"] = remaining
        original["total"]["required_daily_net_sales_remaining"] = (remaining / days_remaining) if (remaining is not None and days_remaining > 0) else (0.0 if remaining == 0 else None)
        original["total"]["forecast_month_end_net_sales"] = total_forecast
        original["total"]["forecast_achievement_pct"] = _pct(total_forecast, target)
        original["total"]["mtd_cm2"] = total_mtd_cm2
        original["total"]["day_cm2"] = total_day_cm2
        original["total"]["forecast_month_end_cm2"] = total_forecast_cm2
        original["total"]["v4_7_5_total_policy"] = "TOTAL includes GT-ex-NV001 + NV001. Monthly company target is unchanged."
    original["v4_7_5_policy"] = {
        "gt_actual_excludes_employee_code": SPECIAL_EMPLOYEE_CODE,
        "special_employee_target": None,
        "company_total_includes_special_employee": True,
    }
    return original


def _append_warning(package: MutableMapping[str, Any], text: str) -> None:
    warnings = package.setdefault("data_quality", {}).setdefault("warnings", [])
    if text not in warnings:
        warnings.append(text)


def _append_error(package: MutableMapping[str, Any], text: str) -> None:
    errors = package.setdefault("data_quality", {}).setdefault("errors", [])
    if text not in errors:
        errors.append(text)


def _refresh_dq(package: MutableMapping[str, Any]) -> None:
    dq = package.setdefault("data_quality", {})
    if dq.get("errors"):
        dq["status"] = "ERROR"; dq["can_send"] = False
    elif dq.get("warnings"):
        dq["status"] = "WARNING"; dq["can_send"] = True
    else:
        dq["status"] = "PASS"; dq["can_send"] = True


def postprocess_v475(base, v474, package: MutableMapping[str, Any], report_date: dt.date, compare_date: dt.date) -> None:
    mtd_start = report_date.replace(day=1)
    periods = (package.get("metadata") or {}).get("mtd_comparison_periods") or {}
    prev_start = dt.date.fromisoformat(str(periods.get("previous_start") or (report_date.replace(day=1) - dt.timedelta(days=1)).replace(day=1)))
    prev_end = dt.date.fromisoformat(str(periods.get("previous_end") or (report_date.replace(day=1) - dt.timedelta(days=1))))
    query_start = min(mtd_start, prev_start, compare_date)

    try:
        import psycopg2.extras
    except Exception as exc:
        raise RuntimeError("Missing psycopg2-binary required by v4.7.5") from exc

    with base.connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            all_staff_daily, staff_audit = _fetch_special_daily(base, cur, query_start, report_date)
            staff_sku = _fetch_special_sku_daily(base, cur, mtd_start, report_date)

    staff_map = _daily_map(all_staff_daily)
    current_staff = [staff_map[d.isoformat()] for d in _date_range(mtd_start, report_date) if d.isoformat() in staff_map]
    prev_staff = [staff_map[d.isoformat()] for d in _date_range(prev_start, prev_end) if d.isoformat() in staff_map]

    current_daily = _split_daily_detail(package.get("mtd_daily_detail_by_channel") or {}, current_staff, mtd_start, report_date)
    previous_daily = _split_daily_detail(package.get("previous_mtd_daily_detail_by_channel") or {}, prev_staff, prev_start, prev_end)
    comparison_by_channel, comparison_total = _build_reporting_mtd_comparison(base, current_daily, previous_daily, report_date, prev_end)

    # Current/compare-day reporting rows.
    legacy_daily_map = package.get("daily_pnl_by_channel_map") or {}
    report_staff = staff_map.get(report_date.isoformat()) or _zero_special_row(report_date)
    compare_staff = staff_map.get(compare_date.isoformat()) or _zero_special_row(compare_date)
    reporting_daily_map = copy.deepcopy(dict(legacy_daily_map))
    legacy_gt = legacy_daily_map.get("GT") or {}
    reporting_daily_map["GT"] = {
        "channel": "GT",
        "current": _subtract_staff_from_gt(legacy_gt.get("current") or {}, report_staff),
        "previous": _subtract_staff_from_gt(legacy_gt.get("previous") or {}, compare_staff),
    }
    reporting_daily_map[SPECIAL_CHANNEL_KEY] = {
        "channel": SPECIAL_CHANNEL_KEY,
        "display_channel": SPECIAL_CHANNEL_DISPLAY,
        "parent_channel": "GT",
        "current": report_staff,
        "previous": compare_staff,
        "pnl_lines": base.pnl_lines(report_staff, compare_staff),
    }
    reporting_daily_map["GT"]["pnl_lines"] = base.pnl_lines(reporting_daily_map["GT"]["current"], reporting_daily_map["GT"]["previous"])

    reporting_daily_list = [reporting_daily_map[ch] for ch in REPORTING_CHANNELS if ch in reporting_daily_map]
    total_current = dict(base.aggregate([reporting_daily_map[ch]["current"] for ch in REPORTING_CHANNELS], report_date)); total_current["channel"] = "TOTAL"
    total_previous = dict(base.aggregate([reporting_daily_map[ch]["previous"] for ch in REPORTING_CHANNELS], compare_date)); total_previous["channel"] = "TOTAL"

    # SKU/inventory reporting split.
    reporting_sku_fact = _split_sku_fact(package, staff_sku)
    v47 = _load_v47_from_v474(v474)
    reporting_sku_summary = v47._build_sku_inventory_summary(reporting_sku_fact, package.get("inventory") or {}, report_date, company_level=False)
    reporting_company_sku = v47._build_sku_inventory_summary(reporting_sku_fact, package.get("inventory") or {}, report_date, company_level=True)
    v47._attach_inventory_alerts_to_sku_summaries(reporting_sku_summary, package.get("inventory") or {})
    v47._attach_inventory_alerts_to_sku_summaries(reporting_company_sku, package.get("inventory") or {})
    reporting_top20 = _build_top20_from_summary(reporting_sku_summary)
    reporting_gifts = _build_gift_view(reporting_sku_summary)

    package["reporting_mtd_daily_detail_by_channel_v4_7_5"] = current_daily
    package["reporting_previous_mtd_daily_detail_by_channel_v4_7_5"] = previous_daily
    package["reporting_mtd_pnl_comparison_by_channel_v4_7_5"] = comparison_by_channel
    package["reporting_mtd_pnl_comparison_total_v4_7_5"] = comparison_total
    package["reporting_daily_pnl_by_channel_map_v4_7_5"] = reporting_daily_map
    package["reporting_daily_pnl_by_channel_v4_7_5"] = reporting_daily_list
    package["reporting_daily_summary_v4_7_5"] = {"current": total_current, "previous": total_previous}
    package["reporting_target_progress_v4_7_5"] = _rebuild_target_progress(package, current_daily, report_date, base)
    package["reporting_sku_daily_fact_v4_7_5"] = reporting_sku_fact
    package["reporting_sku_inventory_summary_v4_7_5"] = reporting_sku_summary
    package["reporting_company_sku_inventory_summary_v4_7_5"] = reporting_company_sku
    package["reporting_top_20_sku_by_channel_v4_7_5"] = reporting_top20
    package["reporting_gift_sku_mtd_by_channel_v4_7_5"] = reporting_gifts
    package["gt_special_employee_v4_7_5"] = {
        "employee_code": SPECIAL_EMPLOYEE_CODE,
        "employee_name": SPECIAL_EMPLOYEE_NAME,
        "reporting_channel": SPECIAL_CHANNEL_KEY,
        "display_name": SPECIAL_CHANNEL_DISPLAY,
        "parent_channel": "GT",
        "policy": {
            "exclude_from_displayed_gt_total": True,
            "include_in_company_total": True,
            "cost_policy": "same GT operating cost rules",
            "shipping_rate": GT_SHIPPING_RATE,
            "backoffice_rate": GT_BACKOFFICE_RATE,
            "advertising_cost": 0,
        },
        "current_mtd": comparison_by_channel[SPECIAL_CHANNEL_KEY]["current_mtd"],
        "previous_mtd": comparison_by_channel[SPECIAL_CHANNEL_KEY]["previous_mtd"],
        "daily_detail_current_mtd": current_daily[SPECIAL_CHANNEL_KEY],
        "source_audit": staff_audit,
    }

    meta = package.setdefault("metadata", {})
    meta["package_version"] = PACKAGE_VERSION
    meta["package_version_int"] = PACKAGE_VERSION_INT
    meta["upgrade_scope_v4_7_5"] = "GT employee NV001 reporting split; normal GT costs; no ads; company total preserved"

    contract = package.setdefault("excel_render_contract", {})
    contract["v4_7_5_gt_special_employee"] = {
        "sheet": "01_GT",
        "main_gt_source": "reporting_mtd_daily_detail_by_channel_v4_7_5.GT",
        "special_employee_source": f"reporting_mtd_daily_detail_by_channel_v4_7_5.{SPECIAL_CHANNEL_KEY}",
        "special_employee_block": "gt_special_employee_v4_7_5",
        "target_source": "reporting_target_progress_v4_7_5",
        "mtd_comparison_source": "reporting_mtd_pnl_comparison_by_channel_v4_7_5",
        "sku_source": "reporting_sku_inventory_summary_v4_7_5",
        "top20_source": "reporting_top_20_sku_by_channel_v4_7_5",
        "gift_source": "reporting_gift_sku_mtd_by_channel_v4_7_5",
        "channel_order": list(REPORTING_CHANNELS),
        "legacy_warning": "Do not use inherited v4.7.4 GT blocks for 01_GT after v4.7.5; they still include NV001 for backward compatibility.",
    }
    policy = package.setdefault("report_rendering_policy", {})
    policy["gt_special_employee_v4_7_5"] = {
        "display_gt_excluding_employee": SPECIAL_EMPLOYEE_CODE,
        "render_special_employee_as_separate_section_under_gt": True,
        "special_employee_display_name": SPECIAL_CHANNEL_DISPLAY,
        "special_employee_ads": 0,
        "company_total_includes_special_employee": True,
    }
    package.setdefault("pnl_rules", {})["v4_7_5_gt_nv001"] = (
        "NV001/NGUYỄN XUÂN MẠNH remains GT in source data, is excluded only from displayed GT reporting totals, "
        "is reported separately with normal GT COGS/logistics/backoffice costs, and receives zero advertising cost."
    )

    # Strict reconciliation: no sales/cost may disappear or double count.
    dq = package.setdefault("data_quality", {})
    audit = {
        "employee_source": staff_audit,
        "mtd_gt_original_equals_gt_ex_plus_nv001": {},
        "company_total_preserved": {},
        "special_ads_zero": True,
    }
    legacy_gt_mtd = dict(base.aggregate((package.get("mtd_daily_detail_by_channel") or {}).get("GT") or [], report_date))
    new_gt_mtd = comparison_by_channel["GT"]["current_mtd"]
    special_mtd = comparison_by_channel[SPECIAL_CHANNEL_KEY]["current_mtd"]
    for field in RECON_FIELDS:
        gap = _f(legacy_gt_mtd.get(field)) - _f(new_gt_mtd.get(field)) - _f(special_mtd.get(field))
        audit["mtd_gt_original_equals_gt_ex_plus_nv001"][field] = gap
        if abs(gap) > EPSILON:
            _append_error(package, f"v4.7.5 GT split reconciliation failed for {field}: gap={gap:,.2f}")
    legacy_total = package.get("previous_mtd_summary")  # just existence sanity; current total comes from v4.7.3 comparison total
    legacy_current_total = ((package.get("mtd_pnl_comparison_total") or {}).get("current_mtd") or {})
    for field in RECON_FIELDS:
        gap = _f(legacy_current_total.get(field)) - _f(comparison_total["current_mtd"].get(field))
        audit["company_total_preserved"][field] = gap
        if abs(gap) > EPSILON:
            _append_error(package, f"v4.7.5 company total changed after GT/NV001 split for {field}: gap={gap:,.2f}")
    if abs(_f(special_mtd.get("ads_total"))) > EPSILON:
        audit["special_ads_zero"] = False
        _append_error(package, "v4.7.5 NV001 advertising cost is not zero.")
    if staff_audit.get("available") is False:
        _append_error(package, f"v4.7.5 cannot read NV001 source: {staff_audit.get('reason')}")
    if staff_audit.get("name_match") is False:
        _append_error(package, f"v4.7.5 NV001 name mismatch: observed={staff_audit.get('observed_employee_names')}")
    if not current_staff:
        _append_warning(package, f"v4.7.5 found no current-MTD rows for {SPECIAL_EMPLOYEE_CODE}; special row is zero-filled.")
    dq["v4_7_5_gt_special_employee"] = audit
    _refresh_dq(package)


def build_package_v475(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    alias_file: Path,
    bundle_file: Path,
    include_growth_context: bool = True,
    require_targets: bool = True,
    require_inventory: bool = True,
    require_top20_mapping: bool = True,
):
    v474 = _load_v474()
    base, package = v474.build_package_v474(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        alias_file,
        bundle_file,
        include_growth_context=include_growth_context,
        require_targets=require_targets,
        require_inventory=require_inventory,
        require_top20_mapping=require_top20_mapping,
    )
    report_date = base.parse_date(report_date_str)
    compare_date = base.resolve_compare_date(report_date, compare_mode, explicit_compare_date)
    postprocess_v475(base, v474, package, report_date, compare_date)
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    return base, base.clean(package)


def self_test() -> int:
    failures: List[str] = []
    raw = {
        "report_date": "2026-09-30",
        "gross_sales": 60000004.8,
        "net_sales": 60000000,
        "sold_cogs": 50154802,
        "gift_cogs": 0,
        "orders": 2,
    }
    staff = _staff_pnl_from_raw(raw)
    if abs(staff["logistics_order_cost"] - 1200000) > 1e-6:
        failures.append("NV001 shipping must be 2% of Net Sales")
    if abs(staff["backoffice_cost"] - 9000000) > 1e-6:
        failures.append("NV001 backoffice must be 15% of Net Sales")
    if staff["ads_total"] != 0:
        failures.append("NV001 ads must be zero")
    gt = copy.deepcopy(staff)
    gt["channel"] = "GT"
    for k in ADDITIVE_PNL_FIELDS:
        if isinstance(gt.get(k), (int,float)):
            gt[k] = _f(gt[k]) * 2
    gt["orders"] = 4; gt["success_orders"] = 4
    _recalc_pnl(gt)
    residual = _subtract_staff_from_gt(gt, staff)
    for field in ("net_sales", "cogs_total", "logistics_order_cost", "backoffice_cost", "profit"):
        if abs((_f(residual.get(field)) + _f(staff.get(field))) - _f(gt.get(field))) > EPSILON:
            failures.append(f"GT split reconciliation failed in fixture: {field}")
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for f in failures: print("-", f, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("NV001 separate GT reporting: PASS")
    print("NV001 normal GT costs (2% logistics + 15% BO): PASS")
    print("NV001 advertising = 0: PASS")
    print("GT original = GT excluding NV001 + NV001: PASS")
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
    ap.add_argument("--allow-unmapped-top20", action="store_true")
    ap.add_argument("--sku-map", default="config/product_sku_alias_master_v4_7_4.csv")
    ap.add_argument("--bundle-map", default="config/bundle_component_map_v4_7_4.csv")
    ap.add_argument("--out-dir", default="outbox/packages")
    ap.add_argument("--store-db", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.date:
        ap.error("--date is required unless --self-test is used")

    v474 = _load_v474()
    v473 = v474._load_v473(); v472 = v473._load_v472(); v471 = v472._load_v471(); v47 = v471._load_v47(); v461 = v47._load_v461(); base45 = v461._load_v46()._load_base()
    try:
        report_date = base45.parse_date(args.date).isoformat()
        vat_rates = base45.parse_vat_rates(args.vat_rates)
        if args.compare_date: base45.parse_date(args.compare_date)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr); return 2
    if args.require_vat_rates:
        missing = [ch for ch in base45.DAILY_PNL_SHEETS if ch not in vat_rates]
        if missing:
            print("ERROR: missing VAT rates for " + ", ".join(missing), file=sys.stderr); return 2

    root = Path(__file__).resolve().parents[1]
    alias_file = Path(args.sku_map); alias_file = alias_file if alias_file.is_absolute() else root / alias_file
    bundle_file = Path(args.bundle_map); bundle_file = bundle_file if bundle_file.is_absolute() else root / bundle_file
    base, package = build_package_v475(
        report_date, args.compare_mode, args.compare_date, vat_rates, alias_file, bundle_file,
        include_growth_context=not args.without_growth_context,
        require_targets=not args.allow_missing_targets,
        require_inventory=not args.allow_missing_inventory,
        require_top20_mapping=not args.allow_unmapped_top20,
    )
    out_dir = Path(args.out_dir); out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ceo_daily_pnl_package_v4_7_5_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_7_5_{report_date}.md"
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=base.json_default), encoding="utf-8")
    md = base.markdown_summary(package)
    md_path.write_text(md, encoding="utf-8")
    if args.store_db:
        base.PACKAGE_VERSION = PACKAGE_VERSION; base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
        base.store_best_effort(report_date, package, md, str(json_path), str(md_path))
    dq = package.get("data_quality") or {}
    print(f"JSON package: {json_path}")
    print(f"Markdown summary: {md_path}")
    print(f"Data quality: {dq.get('status')}")
    for w in dq.get("warnings") or []: print("WARNING:", w)
    for e in dq.get("errors") or []: print("ERROR:", e)
    return 0 if dq.get("can_send") else 1


if __name__ == "__main__":
    raise SystemExit(main())
