#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict validator for CEO Daily P&L package v4.7.1."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any, List, Mapping, Sequence

PACKAGE_VERSION = "v4.7.1"
PACKAGE_VERSION_INT = 471
BASE_FILENAME = "validate_ceo_daily_pnl_package_v4_7.py"
CHANNELS = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
SALES_CHANNELS = ("NHANH", "SHOPEE", "TIKTOK")
TOP_N = 20
EPSILON = 1.0


def _load_base():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7.1 validator layers on v4.7")
    spec = importlib.util.spec_from_file_location("hh_validate_v47", path)
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


def pct(num: Any, den: Any):
    d = f(den)
    return None if abs(d) < 1e-12 else f(num) / d


def validate_package(p: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    meta = p.get("metadata") or {}
    if meta.get("package_version") != PACKAGE_VERSION:
        errors.append(f"package_version={meta.get('package_version')}, expected {PACKAGE_VERSION}")
    if meta.get("package_version_int") != PACKAGE_VERSION_INT:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected {PACKAGE_VERSION_INT}")

    base = _load_base()
    p47 = copy.deepcopy(dict(p))
    p47.setdefault("metadata", {})["package_version"] = "v4.7"
    p47["metadata"]["package_version_int"] = 47
    for e in base.validate_package(p47):
        errors.append(f"v4.7-base: {e}")

    contract = p.get("excel_render_contract") or {}
    if contract.get("top_sku_source") != "top_20_sku_by_channel":
        errors.append("excel_render_contract.top_sku_source must be top_20_sku_by_channel")
    if contract.get("top_sku_limit_per_channel") != TOP_N:
        errors.append("excel_render_contract.top_sku_limit_per_channel must be 20")
    if contract.get("inventory_primary_table_source") != "inventory_sheet_sku_summary":
        errors.append("inventory primary source must be inventory_sheet_sku_summary")

    top = p.get("top_20_sku_by_channel") or {}
    inv_sheet = p.get("inventory_sheet_sku_summary") or []
    inv_by_key = {str(r.get("inventory_key")): r for r in inv_sheet if r.get("inventory_key")}

    for ch in CHANNELS:
        rows = top.get(ch)
        if rows is None:
            errors.append(f"top_20_sku_by_channel missing {ch}")
            continue
        if len(rows) > TOP_N:
            errors.append(f"top_20_sku_by_channel.{ch} has {len(rows)} rows > 20")
        prev_sold = None
        for idx, row in enumerate(rows, start=1):
            sold = f(row.get("sold_qty_mtd"))
            if row.get("rank_by_sold_qty") != idx:
                errors.append(f"top_20_sku_by_channel.{ch}[{idx}] rank is not consecutive")
            if sold <= 0:
                errors.append(f"top_20_sku_by_channel.{ch}[{idx}] must have positive sold_qty_mtd")
            if prev_sold is not None and sold > prev_sold + 1e-9:
                errors.append(f"top_20_sku_by_channel.{ch} is not sorted descending by sold_qty_mtd")
            prev_sold = sold
            if not close(row.get("total_push_qty_mtd"), f(row.get("sold_qty_mtd")) + f(row.get("gift_qty_mtd")), tol=1e-9):
                errors.append(f"top_20_sku_by_channel.{ch}[{idx}] sold+gift != total_push")
            stock = row.get("current_stock_qty")
            ratio = row.get("sold_qty_mtd_to_current_stock_pct")
            if stock is not None and f(stock) > 0:
                expected = pct(sold, stock)
                if ratio is None or abs(f(ratio) - f(expected)) > 1e-12:
                    errors.append(f"top_20_sku_by_channel.{ch}[{idx}] sold/current stock ratio mismatch")
            key = str(row.get("inventory_key") or "")
            if key:
                inv = inv_by_key.get(key)
                if not inv:
                    errors.append(f"top_20_sku_by_channel.{ch}[{idx}] inventory_key not in inventory sheet summary")
                elif row.get("current_stock_qty") is not None and not close(row.get("current_stock_qty"), inv.get("current_stock_qty")):
                    errors.append(f"top_20_sku_by_channel.{ch}[{idx}] stock does not match inventory summary")

    # One row per inventory SKU and stock must reconcile across all expiry dates.
    keys = [str(r.get("inventory_key") or "") for r in inv_sheet]
    nonblank = [k for k in keys if k]
    if len(nonblank) != len(set(nonblank)):
        errors.append("inventory_sheet_sku_summary contains duplicate inventory_key rows")

    for idx, row in enumerate(inv_sheet):
        breakdown = row.get("expiry_breakdown") or []
        breakdown_total = sum(f(x.get("stock_qty")) for x in breakdown)
        if not close(row.get("current_stock_qty"), breakdown_total):
            errors.append(f"inventory_sheet_sku_summary[{idx}] current stock != sum across expiry dates/lots")
        if not close(row.get("stock_qty_sum_all_expiry_dates"), breakdown_total):
            errors.append(f"inventory_sheet_sku_summary[{idx}] stock_qty_sum_all_expiry_dates mismatch")
        three = sum(f(row.get(f"sold_qty_mtd_{ch.lower()}")) for ch in SALES_CHANNELS)
        if not close(row.get("sold_qty_mtd_3_channels"), three, tol=1e-9):
            errors.append(f"inventory_sheet_sku_summary[{idx}] 3-channel sold total mismatch")
        stock = f(row.get("current_stock_qty"))
        ratio = row.get("sold_qty_mtd_3ch_to_current_stock_pct")
        if stock > 0:
            expected = three / stock
            if ratio is None or abs(f(ratio) - expected) > 1e-12:
                errors.append(f"inventory_sheet_sku_summary[{idx}] 3-channel sold/current stock ratio mismatch")

    # Mapping audit must reconcile: mapped + unmapped = total for each requested channel.
    audit = p.get("inventory_sales_mapping_audit") or {}
    total_by_ch = audit.get("total_sales_fact_sold_qty_by_channel") or {}
    mapped_by_ch = audit.get("mapped_sold_qty_by_channel") or {}
    unmapped_by_ch = audit.get("unmapped_sold_qty_by_channel") or {}
    for ch in SALES_CHANNELS:
        if not close(total_by_ch.get(ch), f(mapped_by_ch.get(ch)) + f(unmapped_by_ch.get(ch)), tol=1e-9):
            errors.append(f"inventory_sales_mapping_audit {ch}: mapped+unmapped != total")
        sheet_total = sum(f(r.get(f"sold_qty_mtd_{ch.lower()}")) for r in inv_sheet)
        if not close(sheet_total, mapped_by_ch.get(ch), tol=1e-9):
            errors.append(f"inventory sheet mapped sold quantity for {ch} != mapping audit")

    # Legacy all-SKU data may stay in the package for audit, but renderer must not use it.
    policy = p.get("report_rendering_policy") or {}
    top_policy = policy.get("top_sku") or {}
    if top_policy.get("limit") != TOP_N or top_policy.get("rank_by") != "sold_qty_mtd" or top_policy.get("exclude_gifts_from_rank") is not True:
        errors.append("report_rendering_policy.top_sku must rank Top20 by sold_qty_mtd excluding gifts")
    if top_policy.get("show_stock_only_for_top20") is not True:
        errors.append("report_rendering_policy.top_sku must show stock only in Top20 report table")

    return list(dict.fromkeys(errors))


def self_test() -> int:
    # Focused arithmetic checks. Full structural check runs against a built package.
    failures: List[str] = []
    if pct(10, 50) != 0.2:
        failures.append("ratio")
    if not close(50, 20 + 30):
        failures.append("stock sum")
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
