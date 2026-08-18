#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CEO Daily P&L package v4.7.2 — all MT/GT customers + SKU-first Top20 + expiry-alert inventory sheet.

Layered on v4.7.1. No P&L, cancellation, target, or inventory snapshot math is changed.

New report contract:
1) MT/GT: package ALL customers with transactions from month start through report_date.
2) Channel Top20 SKU: rank strictly by MTD sold_qty (gifts excluded), then map stock SKU-first.
   Stock is the current inventory snapshot total across all lots/expiry dates for that mapped SKU.
3) Sheet 06_Ton_kho_Can_date: render only date/expiry alert SKUs from the existing
   inventory.v_inventory_alerts_ceo_for_package output. No new near-expiry threshold is invented.

Important limits:
- Composite seller SKUs are not silently mapped to one inventory SKU.
- current_stock_qty / sold_qty_mtd is a comparison multiple, not forward cover and not sell-through.
- Near-date pushed quantity is still unavailable without lot-level movement attribution.
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
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

PACKAGE_VERSION = "v4.7.2"
PACKAGE_VERSION_INT = 472
BASE_FILENAME = "build_ceo_daily_pnl_package_v4_7_1.py"
CHANNELS: Tuple[str, ...] = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
MTGT_CHANNELS: Tuple[str, ...] = ("GT", "MT")
TOP_N = 20
EPSILON = 1.0


def _load_v471():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.2 must be installed on top of v4.7.1")
    spec = importlib.util.spec_from_file_location("hh_ceo_daily_pnl_v471_base", path)
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


def _is_composite(value: Any) -> bool:
    return "+" in str(value or "")


def _add_months(value: dt.date, months: int) -> dt.date:
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return dt.date(year, month, min(value.day, last_day))


def _fetch_mtgt_customer_mtd_all(base, report_date: dt.date) -> Dict[str, Any]:
    month_start = report_date.replace(day=1)
    prev_end = _add_months(report_date, -1)
    prev_start = prev_end.replace(day=1)
    result: Dict[str, Any] = {
        "available": False,
        "source_relation": "mtgt.sales_lines",
        "grain": "channel + customer_key, all current-MTD customers",
        "period_start": month_start.isoformat(),
        "period_end": report_date.isoformat(),
        "previous_period_start": prev_start.isoformat(),
        "previous_period_end": prev_end.isoformat(),
        "rows": [],
        "by_channel": {"GT": [], "MT": []},
        "summary_by_channel": {},
        "reconciliation": {},
    }
    try:
        import psycopg2.extras
    except Exception as exc:
        result["error"] = f"Missing psycopg2-binary: {exc}"
        return result

    with base.connect() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if not base.relation_exists(cur, "mtgt", "sales_lines"):
                result["error"] = "Missing mtgt.sales_lines"
                return result
            cols = base.relation_columns(cur, "mtgt", "sales_lines")
            required = {"sale_date", "channel_group", "net_revenue"}
            if not required.issubset(cols):
                result["error"] = f"mtgt.sales_lines missing required columns: {sorted(required - cols)}"
                return result

            def c(name: str, fallback: str) -> str:
                return f"s.{name}" if name in cols else fallback

            customer_code = c("customer_code", "NULL::text")
            customer_name = c("customer_name", "NULL::text")
            document_no = c("document_no", "NULL::text")
            qty_sale = c("quantity_sale", "0::numeric")
            qty_gift = c("quantity_gift", "0::numeric")
            qty_return = c("quantity_return", "0::numeric")
            gross = c("gross_revenue", "0::numeric")
            net = c("net_revenue", "0::numeric")
            line_filter = "AND COALESCE(s.line_type,'') <> 'CVC'" if "line_type" in cols else ""

            def query(start: dt.date, end: dt.date) -> List[Dict[str, Any]]:
                sql = f"""
                    WITH base_rows AS (
                        SELECT
                            upper(btrim(s.channel_group))::text AS channel,
                            CASE
                                WHEN NULLIF(btrim(COALESCE({customer_code},'')), '') IS NOT NULL
                                THEN 'CODE:' || lower(btrim(COALESCE({customer_code},'')))
                                ELSE 'NAME:' || lower(regexp_replace(btrim(COALESCE({customer_name},'')), '[[:space:]]+', ' ', 'g'))
                            END AS customer_key,
                            NULLIF(btrim(COALESCE({customer_code},'')), '')::text AS customer_code,
                            NULLIF(btrim(COALESCE({customer_name},'')), '')::text AS customer_name,
                            s.sale_date::date AS sale_date,
                            {document_no}::text AS document_no,
                            COALESCE({qty_sale},0)::numeric AS quantity_sale,
                            COALESCE({qty_gift},0)::numeric AS quantity_gift,
                            COALESCE({qty_return},0)::numeric AS quantity_return,
                            COALESCE({gross},0)::numeric AS gross_revenue,
                            COALESCE({net},0)::numeric AS net_revenue
                        FROM mtgt.sales_lines s
                        WHERE s.sale_date BETWEEN %s AND %s
                          AND upper(btrim(s.channel_group)) IN ('GT','MT')
                          {line_filter}
                    )
                    SELECT
                        channel,
                        customer_key,
                        MAX(customer_code)::text AS customer_code,
                        MAX(customer_name)::text AS customer_name,
                        COUNT(DISTINCT document_no)::numeric AS documents,
                        SUM(quantity_sale)::numeric AS qty_sale,
                        SUM(quantity_gift)::numeric AS qty_gift,
                        SUM(quantity_return)::numeric AS qty_return,
                        SUM(gross_revenue)::numeric AS gross_sales,
                        SUM(net_revenue)::numeric AS net_sales,
                        MIN(sale_date)::date AS first_purchase_date,
                        MAX(sale_date)::date AS last_purchase_date
                    FROM base_rows
                    GROUP BY channel, customer_key
                    ORDER BY channel, net_sales DESC, customer_key
                """
                return base.fetch_all(cur, sql, (start, end))

            current = query(month_start, report_date)
            previous = query(prev_start, prev_end)

    prev_map = {(str(r.get("channel")), str(r.get("customer_key"))): r for r in previous}
    rows: List[Dict[str, Any]] = []
    by_channel: Dict[str, List[Dict[str, Any]]] = {"GT": [], "MT": []}
    rank_counter = {"GT": 0, "MT": 0}
    for raw in current:
        ch = str(raw.get("channel") or "").upper()
        if ch not in MTGT_CHANNELS:
            continue
        rank_counter[ch] += 1
        prev = prev_map.get((ch, str(raw.get("customer_key")))) or {}
        current_net = _f(raw.get("net_sales"))
        prev_net = _f(prev.get("net_sales"))
        row = {
            "channel": ch,
            "rank_by_net_sales_mtd": rank_counter[ch],
            "customer_key": raw.get("customer_key"),
            "customer_code": raw.get("customer_code"),
            "customer_name": raw.get("customer_name") or raw.get("customer_code") or "KHÔNG XÁC ĐỊNH",
            "documents_mtd": _f(raw.get("documents")),
            "qty_sale_mtd": _f(raw.get("qty_sale")),
            "qty_gift_mtd": _f(raw.get("qty_gift")),
            "qty_return_mtd": _f(raw.get("qty_return")),
            "gross_sales_mtd": _f(raw.get("gross_sales")),
            "net_sales_mtd": current_net,
            "first_purchase_date": raw.get("first_purchase_date"),
            "last_purchase_date": raw.get("last_purchase_date"),
            "net_sales_prev_mtd_same_period": prev_net,
            "net_sales_delta": current_net - prev_net,
            "net_sales_delta_pct": _pct(current_net - prev_net, prev_net),
            "comparison_available": bool(prev),
        }
        rows.append(row)
        by_channel[ch].append(row)

    summary: Dict[str, Any] = {}
    for ch in MTGT_CHANNELS:
        ch_rows = by_channel[ch]
        summary[ch] = {
            "customer_count": len(ch_rows),
            "documents_mtd": sum(_f(r.get("documents_mtd")) for r in ch_rows),
            "qty_sale_mtd": sum(_f(r.get("qty_sale_mtd")) for r in ch_rows),
            "qty_gift_mtd": sum(_f(r.get("qty_gift_mtd")) for r in ch_rows),
            "qty_return_mtd": sum(_f(r.get("qty_return_mtd")) for r in ch_rows),
            "gross_sales_mtd": sum(_f(r.get("gross_sales_mtd")) for r in ch_rows),
            "net_sales_mtd": sum(_f(r.get("net_sales_mtd")) for r in ch_rows),
        }
    result.update({
        "available": True,
        "rows": rows,
        "by_channel": by_channel,
        "summary_by_channel": summary,
        "row_count": len(rows),
    })
    return result


def _sku_first_match(row: Mapping[str, Any], catalog, alias_index, pid_index) -> Tuple[Optional[str], str, List[str], Optional[str]]:
    source_skus: List[Any] = list(row.get("source_skus") or [])
    if row.get("source_sku"):
        source_skus.append(row.get("source_sku"))
    if row.get("canonical_sku"):
        source_skus.append(row.get("canonical_sku"))
    if row.get("is_composite_sku") or any(_is_composite(x) for x in source_skus):
        return None, "COMPOSITE_REQUIRES_COMPONENT_ALLOCATION", [], None

    sku_candidates: set[str] = set()
    matched_sku: Optional[str] = None
    for value in source_skus:
        norm = _norm_identifier(value)
        if not norm:
            continue
        hits = set(alias_index.get(norm) or set())
        if hits:
            sku_candidates |= hits
            if matched_sku is None:
                matched_sku = str(value)
    if len(sku_candidates) == 1:
        key = next(iter(sku_candidates))
        return key, "MAPPED_EXACT_SKU", [key], matched_sku
    if len(sku_candidates) > 1:
        return None, "AMBIGUOUS_SKU", sorted(sku_candidates), matched_sku

    ean_candidates: set[str] = set()
    matched_ean: Optional[str] = None
    for value in (row.get("primary_ean"), row.get("ean")):
        norm = _norm_identifier(value)
        if not norm:
            continue
        hits = set(alias_index.get(norm) or set())
        if hits:
            ean_candidates |= hits
            if matched_ean is None:
                matched_ean = str(value)
    if len(ean_candidates) == 1:
        key = next(iter(ean_candidates))
        return key, "MAPPED_EXACT_EAN", [key], matched_ean
    if len(ean_candidates) > 1:
        return None, "AMBIGUOUS_EAN", sorted(ean_candidates), matched_ean

    pid = str(row.get("product_id") or "")
    if pid and pid in pid_index:
        return pid_index[pid], "MAPPED_PRODUCT_ID_FALLBACK", [pid_index[pid]], pid
    return None, "UNMAPPED", [], None


def _build_top20_sku_first(package: Mapping[str, Any]) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    v471 = _load_v471()
    catalog, alias_index, pid_index = v471._inventory_catalog(package)
    grouped: Dict[str, List[Mapping[str, Any]]] = defaultdict(list)
    for row in package.get("sku_inventory_summary") or []:
        ch = str(row.get("channel") or "").upper()
        if ch in CHANNELS and _f(row.get("sold_qty_mtd")) > 0:
            grouped[ch].append(row)

    inv_snapshot = (package.get("inventory") or {}).get("snapshot_date")
    result: Dict[str, List[Dict[str, Any]]] = {ch: [] for ch in CHANNELS}
    flat: List[Dict[str, Any]] = []
    for ch in CHANNELS:
        all_rows = sorted(
            grouped.get(ch) or [],
            key=lambda r: (-_f(r.get("sold_qty_mtd")), -_f(r.get("total_push_qty_mtd")), str(r.get("canonical_sku") or r.get("source_skus") or "")),
        )
        channel_total_sold = sum(_f(r.get("sold_qty_mtd")) for r in all_rows)
        for rank, src in enumerate(all_rows[:TOP_N], start=1):
            key, status, candidates, matched_value = _sku_first_match(src, catalog, alias_index, pid_index)
            inv = catalog.get(key) if key else None
            sold = _f(src.get("sold_qty_mtd"))
            stock = _f(inv.get("current_stock_qty")) if inv else None
            source_skus = [str(x) for x in (src.get("source_skus") or []) if str(x).strip()]
            display_sku = src.get("canonical_sku") or (source_skus[0] if source_skus else None) or (inv.get("canonical_sku") if inv else None)
            if not inv:
                stock_status = "UNMAPPED"
            elif stock is not None and stock <= 0:
                stock_status = "OUT_OF_STOCK"
            else:
                stock_status = "IN_STOCK"
            row = {
                "channel": ch,
                "rank_by_sold_qty": rank,
                "rank_metric": "sold_qty_mtd_excluding_gifts",
                "mapping_priority": "SKU_FIRST_THEN_EAN_THEN_PRODUCT_ID",
                "sku": display_sku,
                "canonical_sku": src.get("canonical_sku") or (inv.get("canonical_sku") if inv else None),
                "primary_ean": src.get("primary_ean") or (inv.get("primary_ean") if inv else None),
                "source_skus": source_skus,
                "product_id": src.get("product_id") or (inv.get("product_id") if inv else None),
                "product_name": src.get("product_name") or (inv.get("product_name") if inv else None),
                "sold_qty_day": _f(src.get("sold_qty_day")),
                "sold_qty_mtd": sold,
                "gift_qty_day": _f(src.get("gift_qty_day")),
                "gift_qty_mtd": _f(src.get("gift_qty_mtd")),
                "total_push_qty_day": _f(src.get("total_push_qty_day")),
                "total_push_qty_mtd": _f(src.get("total_push_qty_mtd")),
                "channel_sold_qty_share_pct": _pct(sold, channel_total_sold),
                "inventory_snapshot_date": inv_snapshot,
                "inventory_key": key,
                "inventory_link_status": status,
                "inventory_match_value": matched_value,
                "inventory_candidate_keys": candidates,
                "inventory_skus": copy.deepcopy((inv or {}).get("inventory_source_skus") or []),
                "current_stock_qty": stock if inv else None,
                "stock_hn": inv.get("stock_hn") if inv else None,
                "stock_hcm": inv.get("stock_hcm") if inv else None,
                "stock_hcm_nhan_tem": inv.get("stock_hcm_nhan_tem") if inv else None,
                "stock_hcm_total": inv.get("stock_hcm_total") if inv else None,
                "inventory_lot_count": inv.get("lot_count") if inv else None,
                "earliest_expiry_date": inv.get("earliest_expiry_date") if inv else None,
                "sold_qty_mtd_to_current_stock_pct": _pct(sold, stock) if inv and stock is not None else None,
                "current_stock_qty_to_sold_qty_mtd": (_f(stock) / sold) if inv and stock is not None and sold > 0 else None,
                "stock_status": stock_status,
                "ratio_note": "Bán MTD/Tồn hiện tại và Tồn hiện tại/Bán MTD chỉ là so sánh với snapshot hiện tại; không phải sell-through/tồn đầu kỳ.",
            }
            result[ch].append(row)
            flat.append(row)
    return result, flat


def _build_expiry_alert_skus(package: Mapping[str, Any]) -> Dict[str, Any]:
    v471 = _load_v471()
    catalog, alias_index, pid_index = v471._inventory_catalog(package)
    inventory = package.get("inventory") or {}
    alerts = list(inventory.get("alerts") or [])
    lot_detail = list(inventory.get("lot_detail") or [])
    report_date = dt.date.fromisoformat(str((package.get("metadata") or {}).get("report_date")))

    date_alerts = [a for a in alerts if a.get("earliest_expiry_date") not in (None, "")]
    rows: List[Dict[str, Any]] = []
    for alert in date_alerts:
        probe = {
            "source_sku": alert.get("sku_code"),
            "ean": alert.get("ean"),
            "canonical_sku": alert.get("sku_code"),
        }
        key, status, candidates, matched_value = _sku_first_match(probe, catalog, alias_index, pid_index)
        inv = catalog.get(key) if key else None
        expiry_s = str(alert.get("earliest_expiry_date"))
        try:
            expiry_date = dt.date.fromisoformat(expiry_s)
            days_to_expiry = (expiry_date - report_date).days
        except Exception:
            expiry_date = None
            days_to_expiry = None

        matched_lots: List[Mapping[str, Any]] = []
        if key:
            for lot in lot_detail:
                lkey, _lstatus, _lcand, _lval = _sku_first_match(lot, catalog, alias_index, pid_index)
                if lkey == key and str(lot.get("expiry_date") or "") == expiry_s:
                    matched_lots.append(lot)
        by_wh: Dict[str, float] = defaultdict(float)
        lot_nos: set[str] = set()
        for lot in matched_lots:
            by_wh[str(lot.get("warehouse") or "UNALLOCATED")] += _f(lot.get("stock_qty"))
            if lot.get("lot_no"):
                lot_nos.add(str(lot.get("lot_no")))
        row = {
            "priority": alert.get("priority"),
            "alert_type": alert.get("alert_type"),
            "sku": alert.get("sku_code") or (inv.get("canonical_sku") if inv else None),
            "canonical_sku": inv.get("canonical_sku") if inv else alert.get("sku_code"),
            "ean": alert.get("ean") or (inv.get("primary_ean") if inv else None),
            "product_name": alert.get("product_name") or (inv.get("product_name") if inv else None),
            "earliest_expiry_date": expiry_s,
            "days_to_expiry": days_to_expiry,
            "affected_qty": alert.get("affected_qty"),
            "affected_pct": alert.get("affected_pct"),
            "reorder_point_qty": alert.get("reorder_point_qty"),
            "current_stock_qty": (inv.get("current_stock_qty") if inv else alert.get("total_stock")),
            "stock_hn": (inv.get("stock_hn") if inv else alert.get("stock_hn")),
            "stock_hcm": (inv.get("stock_hcm") if inv else alert.get("stock_hcm")),
            "stock_qty_at_alert_expiry_date": sum(_f(x.get("stock_qty")) for x in matched_lots) if matched_lots else None,
            "stock_by_warehouse_at_alert_expiry_date": dict(sorted(by_wh.items())),
            "lot_nos_at_alert_expiry_date": sorted(lot_nos),
            "reason": alert.get("reason"),
            "suggested_action": alert.get("suggested_action"),
            "owner": alert.get("owner"),
            "inventory_key": key,
            "inventory_link_status": status,
            "inventory_match_value": matched_value,
            "inventory_candidate_keys": candidates,
            "lot_match_status": "MATCHED_EXACT_EXPIRY_DATE" if matched_lots else "NO_EXACT_LOT_MATCH",
            "source_rule": "Existing inventory.v_inventory_alerts_ceo_for_package row with non-null earliest_expiry_date; v4.7.2 does not invent expiry thresholds.",
        }
        rows.append(row)

    def sort_key(r: Mapping[str, Any]):
        priority = str(r.get("priority") or "ZZZ")
        days = r.get("days_to_expiry")
        return (priority, 999999 if days is None else int(days), str(r.get("product_name") or ""), str(r.get("sku") or ""))

    rows.sort(key=sort_key)
    return {
        "available": bool(inventory.get("alert_source_available")),
        "source_relation": inventory.get("alert_source_relation") or "inventory.v_inventory_alerts_ceo_for_package",
        "filter_rule": "earliest_expiry_date IS NOT NULL",
        "threshold_policy": "Preserve existing alert-view semantics; no 30/60/90-day threshold is added by v4.7.2.",
        "inventory_snapshot_date": inventory.get("snapshot_date"),
        "source_alert_count": len(alerts),
        "source_date_alert_count": len(date_alerts),
        "row_count": len(rows),
        "rows": rows,
    }


def _reconcile_customers_to_package(package: Mapping[str, Any], customers: MutableMapping[str, Any]) -> None:
    detail = package.get("mtd_daily_detail_by_channel") or {}
    recon: Dict[str, Any] = {}
    for ch in MTGT_CHANNELS:
        canonical = sum(_f(r.get("net_sales")) for r in (detail.get(ch) or [])) if isinstance(detail, dict) else 0.0
        customer_sum = _f(((customers.get("summary_by_channel") or {}).get(ch) or {}).get("net_sales_mtd"))
        recon[ch] = {
            "customer_net_sales_mtd": customer_sum,
            "canonical_mtd_net_sales": canonical,
            "gap": customer_sum - canonical,
        }
    customers["reconciliation"] = recon


def _postprocess_v472(package: MutableMapping[str, Any], base, report_date: dt.date) -> None:
    customers = _fetch_mtgt_customer_mtd_all(base, report_date)
    _reconcile_customers_to_package(package, customers)
    top20, top20_flat = _build_top20_sku_first(package)
    expiry_alerts = _build_expiry_alert_skus(package)

    package["mtgt_customer_mtd_all"] = customers
    package["top_20_sku_by_channel"] = top20
    package["top_20_sku_rows"] = top20_flat
    package["inventory_expiry_alert_skus"] = expiry_alerts

    meta = package.setdefault("metadata", {})
    meta["package_version"] = PACKAGE_VERSION
    meta["package_version_int"] = PACKAGE_VERSION_INT
    meta["upgrade_scope_v4_7_2"] = "All MT/GT MTD customers + SKU-first Top20 MTD sales-to-stock mapping + expiry-alert-only inventory sheet"

    contract = package.setdefault("excel_render_contract", {})
    contract["top_sku_source"] = "top_20_sku_by_channel"
    contract["top_sku_limit_per_channel"] = TOP_N
    contract["top_sku_rank_metric"] = "sold_qty_mtd_excluding_gifts"
    contract["top_sku_inventory_mapping"] = "exact SKU first, exact EAN second, product_id fallback; composite seller SKUs are not collapsed"
    contract["top_sku_columns"] = [
        "rank_by_sold_qty", "sku", "primary_ean", "product_name", "sold_qty_mtd", "gift_qty_mtd",
        "total_push_qty_mtd", "current_stock_qty", "sold_qty_mtd_to_current_stock_pct",
        "current_stock_qty_to_sold_qty_mtd", "stock_status", "earliest_expiry_date",
    ]
    contract["mtgt_customer_source"] = "mtgt_customer_mtd_all.by_channel"
    contract["mtgt_customer_render_rule"] = "01_GT and 02_MT may render ALL current-MTD customers, sorted by net_sales_mtd descending; do not truncate to Top5."
    contract["inventory_primary_table_source"] = "inventory_expiry_alert_skus.rows"
    contract["inventory_primary_table_grain"] = "one row per existing date/expiry alert row; only alerts with earliest_expiry_date are rendered"
    contract["inventory_primary_table_columns"] = [
        "priority", "alert_type", "sku", "ean", "product_name", "earliest_expiry_date", "days_to_expiry",
        "affected_qty", "stock_qty_at_alert_expiry_date", "current_stock_qty", "reason", "suggested_action",
    ]
    contract["inventory_sheet_full_stock_table"] = "DO_NOT_RENDER"

    policy = package.setdefault("report_rendering_policy", {})
    policy["mtgt_customers"] = {
        "source": "mtgt_customer_mtd_all.by_channel",
        "scope": "all customers with current-MTD transactions",
        "sort_by": "net_sales_mtd desc",
        "limit": None,
    }
    policy["top_sku"] = {
        "source": "top_20_sku_by_channel",
        "limit": TOP_N,
        "rank_by": "sold_qty_mtd",
        "exclude_gifts_from_rank": True,
        "inventory_mapping_priority": "SKU_FIRST_THEN_EAN_THEN_PRODUCT_ID",
        "stock_scope": "sum all lots/expiry dates in resolved current snapshot",
    }
    policy["inventory_sheet"] = {
        "sheet": "06_Ton_kho_Can_date",
        "primary_source": "inventory_expiry_alert_skus.rows",
        "show_only_expiry_alert_skus": True,
        "filter_rule": "earliest_expiry_date IS NOT NULL from existing alert view",
        "do_not_render_full_inventory_master": True,
        "do_not_invent_expiry_threshold": True,
    }

    dq = package.setdefault("data_quality", {})
    dq["v4_7_2_scope"] = {
        "mtgt_customer_rows": customers.get("row_count"),
        "mtgt_customer_count_gt": len((customers.get("by_channel") or {}).get("GT") or []),
        "mtgt_customer_count_mt": len((customers.get("by_channel") or {}).get("MT") or []),
        "top20_rows_by_channel": {ch: len(top20.get(ch) or []) for ch in CHANNELS},
        "inventory_expiry_alert_rows": expiry_alerts.get("row_count"),
        "inventory_expiry_alert_source_rows": expiry_alerts.get("source_date_alert_count"),
    }
    warnings = dq.setdefault("warnings", [])
    if not customers.get("available"):
        msg = f"v4.7.2 could not load all MT/GT MTD customers: {customers.get('error') or 'unknown source error'}"
        if msg not in warnings:
            warnings.append(msg)
    for ch, rec in (customers.get("reconciliation") or {}).items():
        if abs(_f(rec.get("gap"))) > EPSILON:
            msg = f"v4.7.2 {ch} all-customer Net Sales does not reconcile to canonical MTD Net Sales; gap={_f(rec.get('gap')):,.2f}."
            if msg not in warnings:
                warnings.append(msg)
    if not expiry_alerts.get("available"):
        msg = "v4.7.2 inventory expiry-alert sheet source is unavailable; do not invent near-date alerts."
        if msg not in warnings:
            warnings.append(msg)

    package.setdefault("display_semantics", {})["current_stock_qty_to_sold_qty_mtd"] = "Current stock snapshot divided by MTD sold quantity; comparison multiple only."
    package.setdefault("pnl_rules", {})["v4_7_2_scope"] = "No P&L math change. v4.7.2 changes MT/GT customer completeness and report-facing SKU/inventory rendering only."

    v47 = _load_v471()._load_v47()
    if hasattr(v47, "_refresh_dq_status"):
        v47._refresh_dq_status(package)


def build_package_v472(
    report_date_str: str,
    compare_mode: str,
    explicit_compare_date: Optional[str],
    vat_rates: Mapping[str, float],
    include_growth_context: bool = True,
    require_targets: bool = True,
    require_inventory: bool = True,
):
    v471 = _load_v471()
    base, package = v471.build_package_v471(
        report_date_str,
        compare_mode,
        explicit_compare_date,
        vat_rates,
        include_growth_context=include_growth_context,
        require_targets=require_targets,
        require_inventory=require_inventory,
    )
    report_date = base.parse_date(report_date_str)
    _postprocess_v472(package, base, report_date)
    base.PACKAGE_VERSION = PACKAGE_VERSION
    base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
    return base, base.clean(package)


def self_test() -> int:
    failures: List[str] = []
    package: Dict[str, Any] = {
        "metadata": {"report_date": "2026-08-17"},
        "inventory": {
            "snapshot_date": "2026-08-17",
            "alert_source_available": True,
            "alert_source_relation": "inventory.v_inventory_alerts_ceo_for_package",
            "alerts": [{
                "priority": "P1", "alert_type": "NEAR_EXPIRY", "sku_code": "SKU1", "ean": "123",
                "product_name": "Product 1", "total_stock": 50, "affected_qty": 20,
                "earliest_expiry_date": "2026-09-01", "reason": "test", "suggested_action": "push",
            }],
            "lot_detail": [
                {"product_id": "P1", "source_sku": "SKU1", "ean": "123", "canonical_sku": "SKU1", "primary_ean": "123", "warehouse": "HN", "lot_no": "L1", "expiry_date": "2026-09-01", "stock_qty": 20},
                {"product_id": "P1", "source_sku": "SKU1", "ean": "123", "canonical_sku": "SKU1", "primary_ean": "123", "warehouse": "HCM", "lot_no": "L2", "expiry_date": "2026-12-01", "stock_qty": 30},
            ],
        },
        "inventory_sku_master": [{
            "inventory_key": "P1", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123",
            "product_name": "Product 1", "current_stock_qty": 50, "stock_hn": 20, "stock_hcm": 30,
            "stock_hcm_nhan_tem": 0, "stock_hcm_total": 30, "lot_count": 2,
        }],
        "sku_inventory_summary": [{
            "channel": "SHOPEE", "product_id": "P1", "canonical_sku": "SKU1", "primary_ean": "123",
            "source_skus": ["SKU1"], "product_name": "Product 1", "sold_qty_day": 5, "sold_qty_mtd": 10,
            "gift_qty_day": 0, "gift_qty_mtd": 2, "total_push_qty_day": 5, "total_push_qty_mtd": 12,
        }],
    }
    top, _ = _build_top20_sku_first(package)
    row = top["SHOPEE"][0]
    if row.get("inventory_link_status") != "MAPPED_EXACT_SKU":
        failures.append("Top20 must prefer exact SKU mapping")
    if abs(_f(row.get("current_stock_qty")) - 50) > 1e-9:
        failures.append("Top20 current stock must sum all expiry lots")
    if abs(_f(row.get("sold_qty_mtd_to_current_stock_pct")) - 0.2) > 1e-9:
        failures.append("sold/current stock ratio")
    if abs(_f(row.get("current_stock_qty_to_sold_qty_mtd")) - 5.0) > 1e-9:
        failures.append("current stock/sold ratio")
    alerts = _build_expiry_alert_skus(package)
    if alerts.get("row_count") != 1:
        failures.append("expiry alert filter")
    elif abs(_f(alerts["rows"][0].get("stock_qty_at_alert_expiry_date")) - 20) > 1e-9:
        failures.append("expiry-date lot aggregation")
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for x in failures:
            print("-", x, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    print("Top20 MTD sold ranking + SKU-first stock mapping: PASS")
    print("All-lot stock total on Top20: PASS")
    print("Expiry-alert-only inventory sheet source: PASS")
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

    v471 = _load_v471()
    v47 = v471._load_v47()
    v461 = v47._load_v461()
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

    base, package = build_package_v472(
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
    json_path = out_dir / f"ceo_daily_pnl_package_v4_7_2_{report_date}.json"
    md_path = out_dir / f"ceo_daily_pnl_package_v4_7_2_{report_date}.md"
    json_path.write_text(json.dumps(package, ensure_ascii=False, indent=2, default=base.json_default), encoding="utf-8")
    md = base.markdown_summary(package)
    mtgt = package.get("mtgt_customer_mtd_all") or {}
    inv = package.get("inventory_expiry_alert_skus") or {}
    md += "\n\n## v4.7.2 Customer / SKU / Expiry Alert Upgrade\n"
    md += f"- GT MTD customers: {len((mtgt.get('by_channel') or {}).get('GT') or [])}\n"
    md += f"- MT MTD customers: {len((mtgt.get('by_channel') or {}).get('MT') or [])}\n"
    md += f"- Top SKU: Top {TOP_N}/channel by sold_qty_mtd, inventory mapping SKU-first\n"
    md += f"- Expiry alert rows for sheet 06: {inv.get('row_count')}\n"
    md_path.write_text(md, encoding="utf-8")
    if args.store_db:
        base.PACKAGE_VERSION = PACKAGE_VERSION
        base.PACKAGE_VERSION_INT = PACKAGE_VERSION_INT
        base.store_best_effort(report_date, package, md, str(json_path), str(md_path))

    dq = package.get("data_quality") or {}
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
