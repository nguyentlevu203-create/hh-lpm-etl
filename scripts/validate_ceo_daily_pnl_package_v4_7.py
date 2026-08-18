#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict validator for CEO Daily P&L package v4.7."""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import importlib.util
import json
import math
import sys
from pathlib import Path
from typing import Any, List, Mapping, Sequence

PACKAGE_VERSION = "v4.7"
PACKAGE_VERSION_INT = 47
BASE_FILENAME = "validate_ceo_daily_pnl_package_v4_6_1.py"
CHANNELS = ("GT", "MT", "NHANH", "SHOPEE", "TIKTOK")
EXPECTED_SHEETS = ["00_Tong_quan", "01_GT", "02_MT", "03_Nhanh", "04_Shopee", "05_TikTok", "06_Ton_kho_Can_date"]
EPSILON = 1.0


def _load_base_validator():
    path = Path(__file__).resolve().with_name(BASE_FILENAME)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}; v4.7 validator layers on v4.6.1 validator")
    spec = importlib.util.spec_from_file_location("hh_validate_v461", path)
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


def add_months(value: dt.date, months: int) -> dt.date:
    import calendar
    year = value.year + (value.month - 1 + months) // 12
    month = (value.month - 1 + months) % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return dt.date(year, month, day)


def contains_nd(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().upper() in {"N/D", "ND", "N/A", "N.A."}
    if isinstance(value, Mapping):
        return any(contains_nd(v) for v in value.values())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(contains_nd(v) for v in value)
    return False


def validate_pnl_row(row: Mapping[str, Any], prefix: str, errors: List[str]) -> None:
    if not row:
        errors.append(f"{prefix} missing")
        return
    checks = [
        ("gross split", f(row.get("gross_sales")), f(row.get("gross_non_cancelled_sales")) + f(row.get("gross_cancelled_sales"))),
        ("sales bridge", f(row.get("gross_sales")) - f(row.get("net_sales")), f(row.get("sales_deductions_total"))),
        ("COGS split", f(row.get("cogs_total")), f(row.get("sold_cogs")) + f(row.get("gift_cogs"))),
        ("GM1", f(row.get("gm1")), f(row.get("net_sales")) - f(row.get("cogs_total"))),
        ("CM1", f(row.get("cm1")), f(row.get("gm1")) - f(row.get("cm1_cost_total"))),
        ("CM2", f(row.get("cm2")), f(row.get("cm1")) - f(row.get("cm2_cost_total"))),
        ("Profit", f(row.get("profit")), f(row.get("cm2")) - f(row.get("backoffice_cost"))),
    ]
    for label, actual, expected in checks:
        if abs(actual - expected) > EPSILON:
            errors.append(f"{prefix} {label} mismatch: actual={actual:.2f}, expected={expected:.2f}")


def validate_package(p: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    meta = p.get("metadata") or {}
    if meta.get("package_version") != PACKAGE_VERSION:
        errors.append(f"package_version={meta.get('package_version')}, expected {PACKAGE_VERSION}")
    if meta.get("package_version_int") != PACKAGE_VERSION_INT:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected {PACKAGE_VERSION_INT}")

    # Reuse the entire v4.6.1 financial/status contract.
    base = _load_base_validator()
    p461 = copy.deepcopy(dict(p))
    p461.setdefault("metadata", {})["package_version"] = "v4.6.1"
    p461["metadata"]["package_version_int"] = 461
    for e in base.validate_package(p461):
        errors.append(f"v4.6.1-base: {e}")

    if contains_nd(p):
        errors.append("Package contains N/D or N/A strings; v4.7 requires null/hide for unavailable metrics and numeric 0 for confirmed no-sales")

    # Excel contract.
    contract = p.get("excel_render_contract") or {}
    if contract.get("sheet_order") != EXPECTED_SHEETS:
        errors.append(f"excel_render_contract.sheet_order must be exactly {EXPECTED_SHEETS}")
    if contract.get("pnl_last_line") != "LỢI NHUẬN":
        errors.append("Excel P&L last line must be LỢI NHUẬN")
    if contract.get("forbid_ebitda") is not True:
        errors.append("Excel contract must explicitly forbid EBITDA")

    # Target progress.
    target = p.get("target_progress") or {}
    rows = target.get("rows") or []
    rowmap = {str(r.get("channel")): r for r in rows}
    for ch in CHANNELS:
        if ch not in rowmap:
            errors.append(f"target_progress missing leaf channel {ch}")
    if rows:
        total = target.get("total") or {}
        leaf_target = sum(f(r.get("monthly_target_net_sales")) for r in rows)
        leaf_actual = sum(f(r.get("mtd_net_sales")) for r in rows)
        if not close(total.get("monthly_target_net_sales"), leaf_target):
            errors.append("target_progress TOTAL monthly target does not equal five leaf channel targets")
        if not close(total.get("mtd_net_sales"), leaf_actual):
            errors.append("target_progress TOTAL MTD actual does not equal five leaf channel actuals")
        expected_pct = pct(total.get("mtd_net_sales"), total.get("monthly_target_net_sales"))
        actual_pct = total.get("achievement_mtd_pct")
        if expected_pct is not None and (actual_pct is None or abs(f(actual_pct) - expected_pct) > 1e-12):
            errors.append("target_progress TOTAL achievement_mtd_pct formula mismatch")
        for r in rows:
            expected = pct(r.get("mtd_net_sales"), r.get("monthly_target_net_sales"))
            actual = r.get("achievement_mtd_pct")
            if expected is not None and (actual is None or abs(f(actual) - expected) > 1e-12):
                errors.append(f"target_progress {r.get('channel')} achievement_mtd_pct formula mismatch")
        parent = target.get("parent_audit") or {}
        if parent.get("reconciliation_gap") is not None and abs(f(parent.get("reconciliation_gap"))) > EPSILON:
            errors.append("ECOM target must reconcile to SHOPEE + TIKTOK and must not be double counted")
        if parent.get("must_not_double_count_parent") is not True:
            errors.append("target parent audit must explicitly forbid ECOM double counting")

    # Canonical target MTD actual must match package monthly summary.
    if target.get("total"):
        monthly = p.get("monthly_summary") or {}
        if not close((target.get("total") or {}).get("mtd_net_sales"), monthly.get("net_sales_mtd")):
            errors.append("target_progress TOTAL MTD Net Sales does not match canonical monthly_summary.net_sales_mtd")

    # Full P&L same day previous month.
    prev = p.get("previous_month_same_day_full_pnl") or {}
    try:
        report_date = dt.date.fromisoformat(str(meta.get("report_date")))
        expected_prev = add_months(report_date, -1).isoformat()
        if prev.get("report_date") != expected_prev:
            errors.append(f"previous_month_same_day_full_pnl.report_date={prev.get('report_date')}, expected {expected_prev}")
    except Exception:
        report_date = None
        errors.append("metadata.report_date invalid")
    prev_channels = prev.get("by_channel") or {}
    for ch in CHANNELS:
        entry = prev_channels.get(ch) or {}
        validate_pnl_row(entry.get("current") or {}, f"previous_month_same_day_full_pnl.{ch}", errors)

    # SKU fact equations.
    facts = p.get("sku_daily_fact") or []
    for idx, row in enumerate(facts):
        sold = f(row.get("sold_qty")); gift = f(row.get("gift_qty")); push = f(row.get("total_push_qty"))
        if sold < -1e-9 or gift < -1e-9 or f(row.get("cancelled_qty")) < -1e-9:
            errors.append(f"sku_daily_fact[{idx}] contains negative sold/gift/cancelled quantity")
        if abs(push - (sold + gift)) > 1e-9:
            errors.append(f"sku_daily_fact[{idx}] sold_qty + gift_qty != total_push_qty")
        if abs(f(row.get("total_push_cogs")) - (f(row.get("sold_cogs")) + f(row.get("gift_cogs")))) > EPSILON:
            errors.append(f"sku_daily_fact[{idx}] sold_cogs + gift_cogs != total_push_cogs")
        if row.get("is_composite_sku") and row.get("product_mapping_status") == "MAPPED":
            errors.append(f"sku_daily_fact[{idx}] composite SKU must not be silently mapped to one inventory product")

    # Inventory reconciliation and no future snapshot.
    inv = p.get("inventory") or {}
    if inv.get("available"):
        if report_date is not None:
            try:
                snap = dt.date.fromisoformat(str(inv.get("snapshot_date")))
                if snap > report_date:
                    errors.append("inventory snapshot_date is after report_date (future-data leakage)")
                if inv.get("freshness_days") != (report_date - snap).days:
                    errors.append("inventory freshness_days formula mismatch")
            except Exception:
                errors.append("inventory snapshot_date invalid")
        lot_total = sum(f(r.get("stock_qty")) for r in inv.get("lot_detail") or [])
        wh_total = sum(f(r.get("stock_qty")) for r in inv.get("warehouse_summary") or [])
        sku_total = sum(f(r.get("current_stock_qty")) for r in inv.get("inventory_sku_master") or [])
        summary_total = f((inv.get("summary") or {}).get("total_stock_qty"))
        if not close(lot_total, summary_total):
            errors.append("inventory summary total does not equal lot_detail total")
        if not close(wh_total, summary_total):
            errors.append("inventory warehouse total does not equal lot_detail total")
        if not close(sku_total, summary_total):
            errors.append("inventory SKU total does not equal lot_detail total")
        for idx, sku in enumerate(inv.get("inventory_sku_master") or []):
            if sku.get("stock_by_warehouse"):
                if not close(sku.get("current_stock_qty"), sum(f(v) for v in (sku.get("stock_by_warehouse") or {}).values())):
                    errors.append(f"inventory_sku_master[{idx}] current_stock_qty does not equal stock_by_warehouse total")
            if sku.get("stock_hcm_total") is not None:
                if not close(sku.get("stock_hcm_total"), f(sku.get("stock_hcm")) + f(sku.get("stock_hcm_nhan_tem"))):
                    errors.append(f"inventory_sku_master[{idx}] HCM total != regular HCM + HCM nhan-tem")

    # Per-channel SKU summary equations and stock link consistency.
    inv_by_pid = {str(r.get("product_id")): r for r in p.get("inventory_sku_master") or [] if r.get("product_id")}
    for idx, row in enumerate(p.get("sku_inventory_summary") or []):
        if abs(f(row.get("total_push_qty_day")) - (f(row.get("sold_qty_day")) + f(row.get("gift_qty_day")))) > 1e-9:
            errors.append(f"sku_inventory_summary[{idx}] day push equation mismatch")
        if abs(f(row.get("total_push_qty_mtd")) - (f(row.get("sold_qty_mtd")) + f(row.get("gift_qty_mtd")))) > 1e-9:
            errors.append(f"sku_inventory_summary[{idx}] MTD push equation mismatch")
        pid = str(row.get("product_id") or "")
        if row.get("inventory_link_status") == "MAPPED" and pid:
            inv_row = inv_by_pid.get(pid)
            if not inv_row:
                errors.append(f"sku_inventory_summary[{idx}] claims mapped inventory but product_id not in inventory_sku_master")
            elif not close(row.get("current_stock_qty"), inv_row.get("current_stock_qty")):
                errors.append(f"sku_inventory_summary[{idx}] current_stock_qty does not match inventory master")
        if row.get("near_date_pushed_qty") is not None:
            errors.append(f"sku_inventory_summary[{idx}] near_date_pushed_qty must remain null in v4.7 without lot attribution")

    # Enrichment must never add EBITDA back.
    serialized = json.dumps(p, ensure_ascii=False).lower()
    if "ebitda" in serialized:
        errors.append("EBITDA is forbidden in v4.7")

    return list(dict.fromkeys(errors))


def self_test() -> int:
    failures: List[str] = []
    if add_months(dt.date(2026, 3, 31), -1) != dt.date(2026, 2, 28):
        failures.append("calendar clamp")
    if not contains_nd({"a": [1, "N/D"]}):
        failures.append("N/D scanner")
    if contains_nd({"a": [0, None, "PASS"]}):
        failures.append("N/D false positive")
    row = {
        "gross_sales": 100, "gross_non_cancelled_sales": 90, "gross_cancelled_sales": 10,
        "net_sales": 80, "sales_deductions_total": 20, "sold_cogs": 20, "gift_cogs": 5,
        "cogs_total": 25, "gm1": 55, "cm1_cost_total": 10, "cm1": 45,
        "cm2_cost_total": 15, "cm2": 30, "backoffice_cost": 5, "profit": 25,
    }
    errs: List[str] = []
    validate_pnl_row(row, "fixture", errs)
    if errs:
        failures.extend(errs)
    if failures:
        print("SELF-TEST FAILED", file=sys.stderr)
        for x in failures: print("-", x, file=sys.stderr)
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
        for e in errors: print("-", e, file=sys.stderr)
        return 1
    print("VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
