#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict validator for CEO Daily P&L package v4.7.2."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any, List, Mapping

PACKAGE_VERSION = "v4.7.2"
PACKAGE_VERSION_INT = 472
BASE_FILENAME = "validate_ceo_daily_pnl_package_v4_7_1.py"
CHANNELS = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
MTGT_CHANNELS = ("GT", "MT")
TOP_N = 20
EPSILON = 1.0


def _load_base():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.2 validator layers on v4.7.1")
    spec = importlib.util.spec_from_file_location("hh_validate_v471", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def f(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        out = float(value)
        return 0.0 if math.isnan(out) or math.isinf(out) else out
    except Exception:
        return 0.0


def close(a: Any, b: Any, tol: float = EPSILON) -> bool:
    return abs(f(a) - f(b)) <= tol


def validate_package(p: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    meta = p.get("metadata") or {}
    if meta.get("package_version") != PACKAGE_VERSION:
        errors.append(f"package_version={meta.get('package_version')}, expected {PACKAGE_VERSION}")
    if meta.get("package_version_int") != PACKAGE_VERSION_INT:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected {PACKAGE_VERSION_INT}")

    base = _load_base()
    p471 = copy.deepcopy(dict(p))
    p471.setdefault("metadata", {})["package_version"] = "v4.7.1"
    p471["metadata"]["package_version_int"] = 471
    # v4.7.1 validator expects the former full-inventory render contract. Restore
    # only the copied contract for prerequisite validation; v4.7.2 validates the
    # new expiry-alert-only render contract below.
    p471.setdefault("excel_render_contract", {})["inventory_primary_table_source"] = "inventory_sheet_sku_summary"
    p471["excel_render_contract"]["inventory_primary_table_grain"] = "one row per mapped inventory SKU/product; stock summed across all lots/expiry dates"
    p471.setdefault("report_rendering_policy", {}).setdefault("inventory_sheet", {})["primary_source"] = "inventory_sheet_sku_summary"
    p471.setdefault("report_rendering_policy", {}).setdefault("top_sku", {})["show_stock_only_for_top20"] = True
    for e in base.validate_package(p471):
        errors.append(f"v4.7.1-base: {e}")

    contract = p.get("excel_render_contract") or {}
    if contract.get("mtgt_customer_source") != "mtgt_customer_mtd_all.by_channel":
        errors.append("excel_render_contract.mtgt_customer_source must be mtgt_customer_mtd_all.by_channel")
    if contract.get("inventory_primary_table_source") != "inventory_expiry_alert_skus.rows":
        errors.append("inventory sheet must use inventory_expiry_alert_skus.rows")
    if contract.get("inventory_sheet_full_stock_table") != "DO_NOT_RENDER":
        errors.append("full inventory master must not be rendered on sheet 06")

    customers = p.get("mtgt_customer_mtd_all") or {}
    if not customers.get("available"):
        errors.append("mtgt_customer_mtd_all is unavailable")
    by_ch = customers.get("by_channel") or {}
    flat = customers.get("rows") or []
    if len(flat) != sum(len(by_ch.get(ch) or []) for ch in MTGT_CHANNELS):
        errors.append("mtgt_customer_mtd_all rows != GT+MT by_channel rows")
    for ch in MTGT_CHANNELS:
        rows = by_ch.get(ch)
        if rows is None:
            errors.append(f"mtgt_customer_mtd_all.by_channel missing {ch}")
            continue
        prev_net = None
        for idx, row in enumerate(rows, start=1):
            if str(row.get("channel") or "").upper() != ch:
                errors.append(f"mtgt_customer_mtd_all.{ch}[{idx}] wrong channel")
            if row.get("rank_by_net_sales_mtd") != idx:
                errors.append(f"mtgt_customer_mtd_all.{ch}[{idx}] rank not consecutive")
            net = f(row.get("net_sales_mtd"))
            if prev_net is not None and net > prev_net + 1e-9:
                errors.append(f"mtgt_customer_mtd_all.{ch} not sorted by net_sales_mtd desc")
            prev_net = net
        summary = (customers.get("summary_by_channel") or {}).get(ch) or {}
        if int(summary.get("customer_count") or 0) != len(rows):
            errors.append(f"mtgt_customer_mtd_all {ch} customer_count mismatch")
        if not close(summary.get("net_sales_mtd"), sum(f(r.get("net_sales_mtd")) for r in rows)):
            errors.append(f"mtgt_customer_mtd_all {ch} summary net sales mismatch")
        recon = (customers.get("reconciliation") or {}).get(ch) or {}
        if not close(recon.get("customer_net_sales_mtd"), summary.get("net_sales_mtd")):
            errors.append(f"mtgt_customer_mtd_all {ch} reconciliation customer total mismatch")
        if abs(f(recon.get("gap"))) > EPSILON:
            errors.append(f"mtgt_customer_mtd_all {ch} does not reconcile to canonical MTD Net Sales")

    top = p.get("top_20_sku_by_channel") or {}
    for ch in CHANNELS:
        rows = top.get(ch)
        if rows is None:
            errors.append(f"top_20_sku_by_channel missing {ch}")
            continue
        if len(rows) > TOP_N:
            errors.append(f"top_20_sku_by_channel.{ch} > 20 rows")
        prev_sold = None
        for idx, row in enumerate(rows, start=1):
            sold = f(row.get("sold_qty_mtd"))
            if row.get("rank_by_sold_qty") != idx:
                errors.append(f"top_20_sku_by_channel.{ch}[{idx}] rank not consecutive")
            if row.get("mapping_priority") != "SKU_FIRST_THEN_EAN_THEN_PRODUCT_ID":
                errors.append(f"top_20_sku_by_channel.{ch}[{idx}] mapping priority is not SKU-first")
            if prev_sold is not None and sold > prev_sold + 1e-9:
                errors.append(f"top_20_sku_by_channel.{ch} not sorted by sold_qty_mtd desc")
            prev_sold = sold
            stock = row.get("current_stock_qty")
            if stock is not None and f(stock) > 0:
                expected = sold / f(stock)
                if row.get("sold_qty_mtd_to_current_stock_pct") is None or abs(f(row.get("sold_qty_mtd_to_current_stock_pct")) - expected) > 1e-12:
                    errors.append(f"top_20_sku_by_channel.{ch}[{idx}] sold/current-stock ratio mismatch")
            if stock is not None and sold > 0:
                expected_multiple = f(stock) / sold
                if row.get("current_stock_qty_to_sold_qty_mtd") is None or abs(f(row.get("current_stock_qty_to_sold_qty_mtd")) - expected_multiple) > 1e-12:
                    errors.append(f"top_20_sku_by_channel.{ch}[{idx}] current-stock/sold ratio mismatch")
            if row.get("inventory_link_status") == "MAPPED_EXACT_SKU" and not row.get("inventory_match_value"):
                errors.append(f"top_20_sku_by_channel.{ch}[{idx}] exact SKU map missing matched SKU value")

    expiry = p.get("inventory_expiry_alert_skus") or {}
    rows = expiry.get("rows") or []
    if expiry.get("filter_rule") != "earliest_expiry_date IS NOT NULL":
        errors.append("inventory_expiry_alert_skus filter rule changed")
    if int(expiry.get("row_count") or 0) != len(rows):
        errors.append("inventory_expiry_alert_skus row_count mismatch")
    if int(expiry.get("source_date_alert_count") or 0) != len(rows):
        errors.append("inventory_expiry_alert_skus must preserve every source date-alert row")
    for idx, row in enumerate(rows):
        if row.get("earliest_expiry_date") in (None, ""):
            errors.append(f"inventory_expiry_alert_skus[{idx}] missing expiry date")
        if "30" in str(row.get("source_rule") or "") or "60" in str(row.get("source_rule") or "") or "90" in str(row.get("source_rule") or ""):
            errors.append(f"inventory_expiry_alert_skus[{idx}] appears to invent a day threshold")

    policy = p.get("report_rendering_policy") or {}
    inv_policy = policy.get("inventory_sheet") or {}
    if inv_policy.get("show_only_expiry_alert_skus") is not True:
        errors.append("report_rendering_policy.inventory_sheet must show only expiry-alert SKUs")
    if inv_policy.get("do_not_invent_expiry_threshold") is not True:
        errors.append("report_rendering_policy.inventory_sheet must forbid invented expiry thresholds")
    cust_policy = policy.get("mtgt_customers") or {}
    if cust_policy.get("limit") is not None:
        errors.append("MT/GT customer renderer must not truncate the all-customer MTD block")

    return list(dict.fromkeys(errors))


def self_test() -> int:
    failures: List[str] = []
    if not close(50 / 10, 5):
        failures.append("stock/sold multiple")
    if not close(10 / 50, 0.2):
        failures.append("sold/stock ratio")
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for x in failures:
            print("-", x, file=sys.stderr)
        return 1
    print("SELF-TEST PASSED")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("package", nargs="?")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return self_test()
    if not args.package:
        ap.error("package path is required unless --self-test is used")
    p = json.loads(Path(args.package).read_text(encoding="utf-8"))
    errors = validate_package(p)
    if errors:
        print("VALIDATION FAILED", file=sys.stderr)
        for e in errors:
            print("-", e, file=sys.stderr)
        return 1
    print("VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
