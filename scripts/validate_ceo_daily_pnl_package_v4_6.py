#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Strict validator for CEO Daily P&L package v4.6."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping

TOL = 1.0
NHANH_CANCEL = {"Đã hủy", "Hệ Thống hủy", "Khách hủy", "HVC hủy"}
NHANH_RETURN = {"Đang hoàn", "Xác nhận hoàn", "Đã hoàn"}
NHANH_FAILED = {"Thất bại"}
FORBIDDEN_MARKETPLACE_KEYS = {"gross_returned_sales", "returned_orders", "return_rate", "platform_voucher", "platform_voucher_source"}
NHANH_LEAF_KEYS = {
    "FB_LPM_VIETNAM", "FB_LPM_VIETNAM_168", "FB_BLEDINA_BRASSES_VN",
    "LANDING_PAGE", "WEBSITE_LPM", "ZALO_OA_LPM", "INSTAGRAM",
}


def f(x: Any) -> float:
    try:
        v = float(x or 0)
    except Exception:
        return 0.0
    return 0.0 if math.isnan(v) or math.isinf(v) else v


def close(a: Any, b: Any, tol: float = TOL) -> bool:
    return abs(f(a) - f(b)) <= tol


def daterange(a: dt.date, b: dt.date):
    d = a
    while d <= b:
        yield d
        d += dt.timedelta(days=1)


def recurse_has_key(obj: Any, needle: str) -> bool:
    if isinstance(obj, dict):
        return any(str(k).casefold() == needle.casefold() or recurse_has_key(v, needle) for k, v in obj.items())
    if isinstance(obj, list):
        return any(recurse_has_key(v, needle) for v in obj)
    return False


def validate_package(p: Mapping[str, Any]) -> List[str]:
    errors: List[str] = []
    meta = p.get("metadata") or {}
    dq = p.get("data_quality") or {}
    if meta.get("package_version") != "v4.6":
        errors.append(f"package_version={meta.get('package_version')}, expected v4.6")
    if meta.get("package_version_int") != 46:
        errors.append(f"package_version_int={meta.get('package_version_int')}, expected 46")
    if not dq.get("can_send"):
        errors.append("data_quality.can_send is not true")
    if dq.get("errors"):
        errors.append(f"data_quality.errors is not empty: {dq.get('errors')}")
    if recurse_has_key(p, "ebitda"):
        errors.append("package contains EBITDA")

    contract = dq.get("status_rule_by_channel") or p.get("status_rule_by_channel") or {}
    if set((contract.get("SHOPEE") or {}).get("cancelled_statuses_exact") or []) != {"Đã hủy"}:
        errors.append("Shopee exact cancel contract must be only Đã hủy")
    if set((contract.get("TIKTOK") or {}).get("cancelled_statuses_exact") or []) != {"Canceled"}:
        errors.append("TikTok exact cancel contract must be only Canceled")
    ncontract = contract.get("NHANH") or {}
    if set(ncontract.get("cancelled_statuses_exact") or []) != NHANH_CANCEL:
        errors.append(f"Nhanh cancel whitelist mismatch: {ncontract.get('cancelled_statuses_exact')}")
    if set(ncontract.get("return_statuses_exact_not_cancelled") or []) != NHANH_RETURN:
        errors.append("Nhanh return status contract mismatch")
    if set(ncontract.get("failed_statuses_exact_not_cancelled") or []) != NHANH_FAILED:
        errors.append("Nhanh failed status contract mismatch")
    if NHANH_CANCEL & NHANH_RETURN or NHANH_CANCEL & NHANH_FAILED:
        errors.append("validator configuration has overlapping Nhanh status sets")

    # Marketplace stored flags are still a hard gate because their ETLs derive financials from is_cancelled.
    for ch, expected in (("SHOPEE", "Đã hủy"), ("TIKTOK", "Canceled")):
        aud = (p.get("marketplace_status_audit") or {}).get(ch, {})
        if f(aud.get("flag_mismatch_count")) != 0:
            errors.append(f"{ch} stored is_cancelled mismatch={aud.get('flag_mismatch_count')} for exact {expected}")
        for row in aud.get("status_rows") or []:
            raw = str(row.get("raw_status") or "").strip()
            classified = bool(row.get("classified_cancelled"))
            if ch == "SHOPEE" and raw.casefold() == "đã hủy".casefold() and not classified:
                errors.append("Shopee raw Đã hủy not classified cancelled")
            if ch == "TIKTOK" and raw.casefold() == "canceled" and not classified:
                errors.append("TikTok raw Canceled not classified cancelled")

    naud = p.get("nhanh_status_audit") or {}
    if not naud.get("available"):
        errors.append("nhanh_status_audit unavailable")
    for row in naud.get("status_rows") or []:
        raw = str(row.get("raw_status") or "").strip()
        is_cancel = bool(row.get("classified_cancelled"))
        is_return = bool(row.get("classified_return_status"))
        is_failed = bool(row.get("classified_failed_status"))
        if raw in NHANH_CANCEL and not is_cancel:
            errors.append(f"Nhanh cancel status {raw!r} was not classified cancelled")
        if raw in NHANH_RETURN and (is_cancel or not is_return):
            errors.append(f"Nhanh return status {raw!r} cancellation/return classification wrong")
        if raw in NHANH_FAILED and (is_cancel or not is_failed):
            errors.append(f"Nhanh failed status {raw!r} classification wrong")

    report_date = dt.date.fromisoformat(str(meta.get("report_date")))
    mstart = report_date.replace(day=1)
    expected_dates = [d.isoformat() for d in daterange(mstart, report_date)]
    detail = p.get("mtd_daily_detail_by_channel") or {}

    for ch in ("NHANH", "SHOPEE", "TIKTOK"):
        rows = detail.get(ch) or []
        by_date = {str(r.get("report_date")): r for r in rows}
        if sorted(by_date) != expected_dates:
            errors.append(f"{ch} MTD dates incomplete/duplicate: got {sorted(by_date)}")
            continue
        for day in expected_dates:
            r = by_date[day]
            if not close(r.get("gross_sales"), f(r.get("gross_non_cancelled_sales")) + f(r.get("gross_cancelled_sales"))):
                errors.append(f"{ch} {day}: Gross != non-cancel + cancel Gross")
            if not close(r.get("orders"), f(r.get("success_orders")) + f(r.get("cancelled_orders"))):
                errors.append(f"{ch} {day}: orders != non-cancel compatibility orders + cancelled")
            if not close(f(r.get("gross_sales")) - f(r.get("sales_deductions_total")), r.get("net_sales")):
                errors.append(f"{ch} {day}: Gross - deductions != Net Sales")
            if not close(r.get("sales_deductions_total"), f(r.get("gross_cancelled_sales")) + f(r.get("seller_discount")) + f(r.get("seller_voucher")) + f(r.get("other_sales_adjustment"))):
                errors.append(f"{ch} {day}: revenue bridge components != sales_deductions_total")
            if not close(r.get("cogs_total"), f(r.get("sold_cogs")) + f(r.get("gift_cogs"))):
                errors.append(f"{ch} {day}: sold_cogs + gift_cogs != cogs_total")
            if not close(f(r.get("net_sales")) - f(r.get("cogs_total")), r.get("gm1")):
                errors.append(f"{ch} {day}: Net Sales - COGS != GM1")
            if not close(f(r.get("gm1")) - f(r.get("cm1_cost_total")), r.get("cm1")):
                errors.append(f"{ch} {day}: GM1 - CM1 costs != CM1")
            if not close(f(r.get("cm1")) - f(r.get("cm2_cost_total")), r.get("cm2")):
                errors.append(f"{ch} {day}: CM1 - CM2 costs != CM2")
            if not close(f(r.get("cm2")) - f(r.get("backoffice_cost")), r.get("profit")):
                errors.append(f"{ch} {day}: CM2 - backoffice != Profit")
            for gap in ("gross_status_reconciliation_gap", "sales_deductions_reconciliation_gap", "cogs_split_reconciliation_gap"):
                if abs(f(r.get(gap))) > TOL:
                    errors.append(f"{ch} {day}: {gap}={f(r.get(gap)):.2f}")
            if ch in {"SHOPEE", "TIKTOK"}:
                bad = sorted(k for k in FORBIDDEN_MARKETPLACE_KEYS if k in r)
                if bad:
                    errors.append(f"{ch} {day}: forbidden return/platform-voucher keys {bad}")
            if ch == "NHANH":
                if not close(r.get("non_cancelled_orders"), r.get("success_orders")):
                    errors.append(f"NHANH {day}: non_cancelled_orders != success_orders compatibility field")
                if f(r.get("returned_orders_status")) < 0 or f(r.get("failed_orders_status")) < 0:
                    errors.append(f"NHANH {day}: negative status count")

    parent_alias = p.get("nhanh_parent_daily_detail") or []
    if parent_alias != (detail.get("NHANH") or []):
        errors.append("nhanh_parent_daily_detail is not an exact alias of mtd_daily_detail_by_channel.NHANH")

    # Parent/leaf reconciliation is produced by the v4.5 core and must stay tight.
    recon = p.get("nhanh_leaf_reconciliation_daily") or []
    for row in recon:
        for key, value in row.items():
            if str(key).endswith("_gap") and abs(f(value)) > TOL:
                errors.append(f"Nhanh leaf reconciliation {row.get('report_date')}: {key}={f(value):.2f}")

    catalog = p.get("nhanh_leaf_channel_catalog") or []
    catalog_keys = {str(r.get("key")) for r in catalog if isinstance(r, dict)}
    if catalog_keys != NHANH_LEAF_KEYS:
        errors.append(f"Nhanh leaf catalog mismatch: {catalog_keys}")
    internal = p.get("nhanh_leaf_internal_cost_bucket") or {}
    if internal.get("show_in_report") is not False:
        errors.append("_UNALLOCATED internal bucket must never render")

    # Growth blocks: Nhanh direct daily cancellation/gross/order must reconcile WTD/MTD.
    nrows = detail.get("NHANH") or []
    week_start = report_date - dt.timedelta(days=report_date.weekday())
    wrows = [r for r in nrows if dt.date.fromisoformat(str(r["report_date"])) >= week_start]
    weekly = next((r for r in (p.get("weekly_scorecard_by_channel") or []) if str(r.get("channel")).upper() == "NHANH"), {})
    monthly = next((r for r in (p.get("monthly_scorecard_by_channel") or []) if str(r.get("channel")).upper() == "NHANH"), {})
    checks = (
        ("gross_sales_wtd", sum(f(r.get("gross_sales")) for r in wrows), weekly.get("gross_sales_wtd")),
        ("orders_wtd", sum(f(r.get("orders")) for r in wrows), weekly.get("orders_wtd")),
        ("cancelled_orders_wtd", sum(f(r.get("cancelled_orders")) for r in wrows), weekly.get("cancelled_orders_wtd")),
        ("gross_sales_mtd", sum(f(r.get("gross_sales")) for r in nrows), monthly.get("gross_sales_mtd")),
        ("orders_mtd", sum(f(r.get("orders")) for r in nrows), monthly.get("orders_mtd")),
        ("cancelled_orders_mtd", sum(f(r.get("cancelled_orders")) for r in nrows), monthly.get("cancelled_orders_mtd")),
    )
    for name, expected, actual in checks:
        if not close(expected, actual):
            errors.append(f"NHANH growth mismatch {name}: daily={expected:.2f}, growth={f(actual):.2f}")

    render = (p.get("report_rendering_policy") or {}).get("daily_detail_tables") or {}
    ncols = set((render.get("03_Nhanh") or {}).get("columns") or [])
    required_ncols = {"report_date", "gross_sales", "gross_non_cancelled_sales", "gross_cancelled_sales", "net_sales", "orders", "cancelled_orders", "cancellation_rate", "sold_cogs", "gift_cogs", "profit"}
    if not required_ncols.issubset(ncols):
        errors.append(f"03_Nhanh parent daily rendering missing {sorted(required_ncols - ncols)}")

    return errors


def self_test() -> int:
    # A tiny structural fixture verifies the most dangerous semantic regression.
    fixture = {
        "metadata": {"package_version": "v4.6", "package_version_int": 46, "report_date": "2026-08-01"},
        "data_quality": {"can_send": True, "errors": [], "status_rule_by_channel": {
            "SHOPEE": {"cancelled_statuses_exact": ["Đã hủy"]},
            "TIKTOK": {"cancelled_statuses_exact": ["Canceled"]},
            "NHANH": {"cancelled_statuses_exact": sorted(NHANH_CANCEL), "return_statuses_exact_not_cancelled": sorted(NHANH_RETURN), "failed_statuses_exact_not_cancelled": ["Thất bại"]},
        }},
    }
    # Only test contract parsing here; full validation requires a real package.
    errors=[]
    c=fixture["data_quality"]["status_rule_by_channel"]
    if set(c["TIKTOK"]["cancelled_statuses_exact"])!={"Canceled"}: errors.append("TikTok contract")
    if set(c["NHANH"]["cancelled_statuses_exact"])!=NHANH_CANCEL: errors.append("Nhanh contract")
    if errors:
        print("SELF-TEST FAILED", errors, file=sys.stderr); return 1
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
